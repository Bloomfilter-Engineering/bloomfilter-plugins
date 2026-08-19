from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    UPLOAD_OK,
    UPLOAD_RETRY_BUDGET_S,
    UPLOAD_TOO_LARGE,
    append_to_batch,
    bootstrap_config,
    clear_batch,
    debug_log,
    extract_subagent_conversation,
    get_git_branch,
    is_foreign_runtime_payload,
    read_batch,
    read_payload,
    record_delivered_prefix,
    resolve_api_key,
    resolve_api_url,
    select_uploadable_prefix,
    sweep_stale_batches,
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
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
}
# Upload on Stop (turn end) AND SubagentStop: a subagent can outlive the parent
# turn (Codex doesn't always `wait` on it), so its SubagentStop may fire after
# the parent's final Stop. Uploading on SubagentStop ships the captured
# subagent_transcript regardless. Safe because the BE rebuilds an unfinalized
# turn's events on the later Stop upload (see _handle_turn_start) — the
# SubagentStop upload only materializes the turn's start + subagent anchor.
UPLOAD_HOOKS: set[str] = {"SessionEnd", "Stop", "SubagentStop"}
GIT_BRANCH_HOOKS: set[str] = {"SessionStart", "UserPromptSubmit"}
TRANSCRIPT_EXTRACT_HOOKS: set[str] = {"Stop"}
SESSION_META_HOOKS: set[str] = {"SessionStart"}
# SubagentStop fires under the PARENT session_id and carries the subagent's own
# rollout path (agent_transcript_path); we parse that into a child conversation
# and attach it so the backend builds a linked child AgentSession.
SUBAGENT_STOP_HOOK: str = "SubagentStop"


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

    # Refuse a session that belongs to a different runtime. Editors discover and
    # execute each other's collectors, so this one can be handed hooks from a
    # session it does not serve -- and whichever collector uploads first is the
    # one the whole session gets filed under, so acting on it silently records
    # another tool's work as this one's.
    if is_foreign_runtime_payload(payload):
        debug_log(
            f"hook skipped: hook={hook_event_name} "
            "reason=payload-belongs-to-another-runtime"
        )
        return
    session_id = _resolve_session_id(payload)
    if not session_id:
        return

    project_dir = _resolve_project_dir(payload)
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if hook_event_name == "SessionStart":
        bootstrap_config(plugin_root)
        # Expire batches whose session died before draining them. Swept here and
        # nowhere else: a directory scan costs about a millisecond, fine once per
        # session but not on a per-tool hook that fires thousands of times. A
        # batch is only removed once fully uploaded, so without this the
        # directory grows without limit.
        #
        # Deliberately runs whether or not an API key is configured. Batching
        # happens either way, so gating the only garbage collector behind a key
        # means an install without one keeps every prompt, tool input and tool
        # output on disk in cleartext forever. Nothing recoverable is lost: the
        # age threshold is weeks, and a batch that old belongs to a session that
        # ended long ago.
        #
        # The session being started is passed so its own batch is never treated
        # as stale: at this point it has appended nothing, so a resumed
        # session's file still carries the previous sitting's mtime. Note this
        # runtime then starts the session from an empty batch anyway (below), so
        # here the argument only guarantees the sweep cannot race that reset.
        sweep_stale_batches(current_session_id=session_id)

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
            # Split assistant narration into segments: the LAST segment is the
            # turn's final response (agent_response, rendered at turn end); the
            # earlier segments are intermediate narration ("I'll spin up a
            # subagent…") emitted below as timestamped AgentMessage events so the
            # BE interleaves them around the tool/subagent calls that follow.
            assistant_messages = parsed_turn.get("assistant_messages") or []
            if assistant_messages:
                envelope["agent_response"] = assistant_messages[-1].get("text") or ""
            elif payload.get("last_assistant_message"):
                envelope["agent_response"] = payload["last_assistant_message"]

            for narration in assistant_messages[:-1]:
                narration_text = narration.get("text") or ""
                if not narration_text:
                    continue
                append_to_batch(
                    session_id,
                    {
                        "hook_event_name": "AgentMessage",
                        "received_at": (
                            narration.get("timestamp") or envelope["received_at"]
                        ),
                        "plugin_version": PLUGIN_VERSION,
                        "payload": {
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "text": narration_text,
                            "permission_mode": payload.get("permission_mode", ""),
                        },
                    },
                )

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

    # On SubagentStop, parse the subagent's own rollout (agent_transcript_path)
    # into a normalized child conversation and attach it. Codex fires this hook
    # under the PARENT session_id, so it lands in the parent batch and uploads
    # with the parent on Stop; the backend turns it into a linked child
    # AgentSession. Read NOW — the subagent's rollout may be GC'd later.
    if hook_event_name == SUBAGENT_STOP_HOOK:
        conversation = extract_subagent_conversation(
            payload.get("agent_transcript_path", ""),
            expected_last_message=payload.get("last_assistant_message"),
        )
        if conversation:
            envelope["subagent_transcript"] = conversation

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

        # Cumulative read — never truncate mid-session. This runtime supplies no
        # stable per-turn identity of its own, so a turn is identified by its
        # position in the upload: every upload has to start from the first record
        # or the positions shift under everything already sent. Re-sending is
        # safe; truncating is not.
        batch_entries = read_batch(session_id)
        if not batch_entries:
            debug_log(f"upload skipped: session_id={session_id} reason=empty-batch")
            return

        api_url = resolve_api_url()

        # Send only as much of the batch as fits in one request. Posting the
        # whole file made an oversize batch permanent: the server refuses it,
        # nothing is delivered, later hooks append, and the next attempt is
        # larger still. A prefix is safe here precisely because this runtime
        # never truncates: every upload starts at the first record, so the
        # position of each turn is identical every time and a prefix simply stops
        # short of the newest turns, which the next upload then carries.
        upload_count = select_uploadable_prefix(batch_entries)
        pending_entries = batch_entries[:upload_count]
        batch_payload = {
            "session_id": session_id,
            "source": "codex",
            "plugin_version": PLUGIN_VERSION,
            "hooks": pending_entries,
        }

        # Budget starts before the first request, not after it: the first
        # request can burn the whole socket timeout on its own, so a clock
        # started afterwards lets the worst case run past the hook's limit
        # and be killed mid-flight -- the failure this budget exists to stop.
        retry_deadline = time.monotonic() + UPLOAD_RETRY_BUDGET_S
        upload_result = upload_batch(api_url, api_key, batch_payload)

        while (
            upload_result == UPLOAD_TOO_LARGE
            and len(pending_entries) > 1
            and time.monotonic() < retry_deadline
        ):
            pending_entries = pending_entries[: len(pending_entries) // 2]
            debug_log(
                f"upload retry after too-large: session_id={session_id} "
                f"hooks={len(pending_entries)}"
            )
            batch_payload["hooks"] = pending_entries
            upload_result = upload_batch(api_url, api_key, batch_payload)

        # Record how much of the batch the collector has now seen. A runtime that
        # re-sends everything uses this to know which records belong to turns it
        # has already closed, and so which are safe to shed when the file grows.
        if upload_result == UPLOAD_OK:
            record_delivered_prefix(session_id, len(pending_entries))

        if upload_result != UPLOAD_OK:
            debug_log(
                f"upload incomplete: session_id={session_id} "
                f"result={upload_result} hooks={len(pending_entries)}"
            )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
