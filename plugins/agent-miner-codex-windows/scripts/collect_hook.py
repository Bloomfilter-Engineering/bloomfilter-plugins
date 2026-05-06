from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    append_to_batch,
    bootstrap_config,
    clear_batch,
    debug_log,
    get_git_branch,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    upload_batch,
    utcnow_iso,
)
from codex_rollout import parse_session_meta, parse_turn

SUPPORTED_HOOKS: set[str] = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
}
UPLOAD_HOOKS: set[str] = {"Stop"}
GIT_BRANCH_HOOKS: set[str] = {"SessionStart", "UserPromptSubmit"}
TRANSCRIPT_EXTRACT_HOOKS: set[str] = {"Stop"}
SESSION_META_HOOKS: set[str] = {"SessionStart"}


def _first_string(candidates: list[Any]) -> str:
    """Return the first non-empty string from a list, else ''."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _resolve_project_dir(payload: dict[str, Any]) -> str:
    """Pick the most reliable signal of the user's working directory."""
    workspace_roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    first_workspace_root: str = ""
    if isinstance(workspace_roots, list) and workspace_roots:
        first_workspace_root = (
            workspace_roots[0] if isinstance(workspace_roots[0], str) else ""
        )

    return _first_string(
        [
            payload.get("cwd", ""),
            payload.get("project_dir", ""),
            os.environ.get("CODEX_PROJECT_DIR", ""),
            os.environ.get("CLAUDE_PROJECT_DIR", ""),
            first_workspace_root,
            os.getcwd(),
        ]
    )


def _resolve_session_id(payload: dict[str, Any]) -> str:
    """Pick the session identifier from the payload, preferring `session_id`."""
    return _first_string(
        [
            payload.get("session_id", ""),
            payload.get("conversation_id", ""),
            payload.get("thread_id", ""),
        ]
    )


def main() -> None:
    hook_event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    if hook_event_name not in SUPPORTED_HOOKS:
        return

    payload = read_payload()
    session_id = _resolve_session_id(payload)
    if not session_id:
        return

    project_dir = _resolve_project_dir(payload)
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if hook_event_name == "SessionStart":
        bootstrap_config(plugin_root)
        clear_batch(session_id)

    envelope: dict[str, Any] = {
        "hook_event_name": hook_event_name,
        "received_at": utcnow_iso(),
        "plugin_version": PLUGIN_VERSION,
        "payload": payload,
    }

    if hook_event_name in GIT_BRANCH_HOOKS and project_dir:
        envelope["git_branch"] = get_git_branch(project_dir)

    if hook_event_name == "SessionStart" and project_dir:
        envelope["cwd"] = project_dir

    transcript_path = payload.get("transcript_path", "")
    turn_id = payload.get("turn_id", "")

    # Enrich SessionStart with rollout-level metadata (cli_version, originator).
    # The rollout file usually exists by the time SessionStart fires.
    if (
        hook_event_name in SESSION_META_HOOKS
        and transcript_path
        and os.path.isfile(transcript_path)
    ):
        try:
            session_metadata = parse_session_meta(transcript_path)
        except Exception:
            session_metadata = {}
        for metadata_key, metadata_value in session_metadata.items():
            if metadata_value:
                payload[metadata_key] = metadata_value

    # On Stop, parse the rollout for the just-finished turn and inject the
    # data the BE config can't get from raw hook payloads (assistant_text,
    # token usage, thinking, tool calls).
    if (
        hook_event_name in TRANSCRIPT_EXTRACT_HOOKS
        and transcript_path
        and turn_id
        and os.path.isfile(transcript_path)
    ):
        try:
            parsed_turn: dict[str, Any] | None = parse_turn(transcript_path, turn_id)
        except Exception:
            parsed_turn = None
        if parsed_turn:
            assistant_text = parsed_turn.get("assistant_text") or ""
            if assistant_text:
                envelope["agent_response"] = assistant_text
            elif payload.get("last_assistant_message"):
                envelope["agent_response"] = payload["last_assistant_message"]

            api_calls = parsed_turn.get("api_calls") or []
            time_to_first_token_ms = parsed_turn.get("time_to_first_token_ms")
            if api_calls or time_to_first_token_ms is not None:
                transcript_summary: dict[str, Any] = {"api_calls": api_calls}
                if time_to_first_token_ms is not None:
                    transcript_summary["time_to_first_token_ms"] = (
                        time_to_first_token_ms
                    )
                envelope["transcript_summary"] = transcript_summary

            for thinking_block in parsed_turn.get("thinking_blocks", []) or []:
                append_to_batch(
                    session_id,
                    {
                        "hook_event_name": "Thinking",
                        "received_at": (
                            thinking_block.get("timestamp") or envelope["received_at"]
                        ),
                        "plugin_version": PLUGIN_VERSION,
                        "payload": {
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "encrypted": True,
                            "duration_ms": thinking_block.get("duration_ms"),
                            "reasoning_output_tokens": thinking_block.get(
                                "reasoning_output_tokens"
                            ),
                            "api_call_seq": thinking_block.get("api_call_seq"),
                            "permission_mode": payload.get("permission_mode", ""),
                        },
                    },
                )

            for tool_call in parsed_turn.get("tool_calls", []) or []:
                append_to_batch(
                    session_id,
                    {
                        "hook_event_name": "ToolCall",
                        "received_at": (
                            tool_call.get("timestamp") or envelope["received_at"]
                        ),
                        "plugin_version": PLUGIN_VERSION,
                        "payload": {
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "tool_name": tool_call.get("tool_name", ""),
                            "tool_input": tool_call.get("tool_input"),
                            "tool_output": tool_call.get("tool_output"),
                            "tool_call_id": tool_call.get("tool_call_id", ""),
                            "exit_code": tool_call.get("exit_code"),
                            "duration_ms": tool_call.get("duration_ms"),
                            "permission_mode": payload.get("permission_mode", ""),
                        },
                    },
                )

            for file_edit in parsed_turn.get("file_edits", []) or []:
                append_to_batch(
                    session_id,
                    {
                        "hook_event_name": "FileEdit",
                        "received_at": (
                            file_edit.get("timestamp") or envelope["received_at"]
                        ),
                        "plugin_version": PLUGIN_VERSION,
                        "payload": {
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "tool_name": "apply_patch",
                            "tool_call_id": file_edit.get("tool_call_id", ""),
                            "file_path": file_edit.get("file_path", ""),
                            "file_action": file_edit.get("file_action", "MODIFY"),
                            "structured_patch": file_edit.get("structured_patch", []),
                            "permission_mode": payload.get("permission_mode", ""),
                        },
                    },
                )

    # Fall back to last_assistant_message if the rollout didn't provide one
    # (early Stop, missing transcript, etc.).
    if (
        hook_event_name == "Stop"
        and "agent_response" not in envelope
        and payload.get("last_assistant_message")
    ):
        envelope["agent_response"] = payload["last_assistant_message"]

    append_to_batch(session_id, envelope)

    if hook_event_name in UPLOAD_HOOKS:
        # Codex spawns short-lived utility sessions (e.g. the title generator
        # that runs gpt-5.4-mini in parallel with the real chat) which fire the
        # full SessionStart/UserPromptSubmit/Stop sequence but never produce a
        # rollout. Skip uploading those — they'd land as empty single-turn
        # sessions with no thinking/tool/file activity.
        if not transcript_path:
            debug_log(
                f"upload skipped: session_id={session_id} "
                "reason=utility-session-no-transcript_path"
            )
            clear_batch(session_id)
            return

        api_key = resolve_api_key()
        if not api_key:
            debug_log(f"upload skipped: session_id={session_id} reason=no-api-key")
            return

        # Cumulative read — never truncate mid-session. The BE assumes each
        # upload contains the full session history (turn_number resets to 0
        # and increments through the batch); truncating after upload would
        # cause subsequent uploads to overwrite earlier turns at turn 0.
        # Idempotency is handled BE-side via update_or_create on
        # (session, turn_number).
        batch_entries = read_batch(session_id)
        if not batch_entries:
            debug_log(f"upload skipped: session_id={session_id} reason=empty-batch")
            return

        api_url = resolve_api_url()
        upload_batch(
            api_url,
            api_key,
            {
                "session_id": session_id,
                "source": "codex",
                "plugin_version": PLUGIN_VERSION,
                "hooks": batch_entries,
            },
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
