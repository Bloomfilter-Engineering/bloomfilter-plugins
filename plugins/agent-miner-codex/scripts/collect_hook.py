#!/usr/bin/env python3
"""Universal hook handler for Bloomfilter agent mining (Codex)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    append_to_batch,
    bootstrap_config,
    clear_batch,
    get_git_branch,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    upload_batch,
    utcnow_iso,
)
from codex_rollout import parse_session_meta, parse_turn

SUPPORTED_HOOKS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
}
UPLOAD_HOOKS = {"Stop"}
GIT_BRANCH_HOOKS = {"SessionStart", "UserPromptSubmit"}
TRANSCRIPT_EXTRACT_HOOKS = {"Stop"}
SESSION_META_HOOKS = {"SessionStart"}


def _first_string(values):
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _resolve_project_dir(payload):
    workspace_roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    root = ""
    if isinstance(workspace_roots, list) and workspace_roots:
        root = workspace_roots[0] if isinstance(workspace_roots[0], str) else ""

    return _first_string(
        [
            payload.get("cwd", ""),
            payload.get("project_dir", ""),
            os.environ.get("CODEX_PROJECT_DIR", ""),
            os.environ.get("CLAUDE_PROJECT_DIR", ""),
            root,
            os.getcwd(),
        ]
    )


def _resolve_session_id(payload):
    return _first_string(
        [
            payload.get("session_id", ""),
            payload.get("conversation_id", ""),
            payload.get("thread_id", ""),
        ]
    )


def main():
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

    envelope = {
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
    if hook_event_name in SESSION_META_HOOKS and transcript_path and os.path.isfile(transcript_path):
        try:
            meta = parse_session_meta(transcript_path)
        except Exception:
            meta = {}
        for key, value in meta.items():
            if value:
                payload[key] = value

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
            parsed = parse_turn(transcript_path, turn_id)
        except Exception:
            parsed = None
        if parsed:
            assistant_text = parsed.get("assistant_text") or ""
            if assistant_text:
                envelope["agent_response"] = assistant_text
            elif payload.get("last_assistant_message"):
                envelope["agent_response"] = payload["last_assistant_message"]

            api_calls = parsed.get("api_calls") or []
            if api_calls:
                envelope["transcript_summary"] = {"api_calls": api_calls}

            for thinking in parsed.get("thinking_blocks", []) or []:
                append_to_batch(session_id, {
                    "hook_event_name": "Thinking",
                    "received_at": thinking.get("timestamp") or envelope["received_at"],
                    "plugin_version": PLUGIN_VERSION,
                    "payload": {
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "encrypted": True,
                        "duration_ms": thinking.get("duration_ms"),
                        "permission_mode": payload.get("permission_mode", ""),
                    },
                })

            for tool in parsed.get("tool_calls", []) or []:
                append_to_batch(session_id, {
                    "hook_event_name": "ToolCall",
                    "received_at": tool.get("timestamp") or envelope["received_at"],
                    "plugin_version": PLUGIN_VERSION,
                    "payload": {
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "tool_name": tool.get("tool_name", ""),
                        "tool_input": tool.get("tool_input"),
                        "tool_output": tool.get("tool_output"),
                        "tool_call_id": tool.get("tool_call_id", ""),
                        "exit_code": tool.get("exit_code"),
                        "duration_ms": tool.get("duration_ms"),
                        "permission_mode": payload.get("permission_mode", ""),
                    },
                })

            for edit in parsed.get("file_edits", []) or []:
                append_to_batch(session_id, {
                    "hook_event_name": "FileEdit",
                    "received_at": edit.get("timestamp") or envelope["received_at"],
                    "plugin_version": PLUGIN_VERSION,
                    "payload": {
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "tool_name": "apply_patch",
                        "tool_call_id": edit.get("tool_call_id", ""),
                        "file_path": edit.get("file_path", ""),
                        "file_action": edit.get("file_action", "MODIFY"),
                        "structured_patch": edit.get("structured_patch", []),
                        "permission_mode": payload.get("permission_mode", ""),
                    },
                })

    # Fall back to last_assistant_message if the rollout didn't provide one
    # (early Stop, missing transcript, etc.).
    if hook_event_name == "Stop" and "agent_response" not in envelope and payload.get("last_assistant_message"):
        envelope["agent_response"] = payload["last_assistant_message"]

    append_to_batch(session_id, envelope)

    if hook_event_name in UPLOAD_HOOKS:
        api_key = resolve_api_key()
        if not api_key:
            return

        entries = read_batch(session_id)
        if not entries:
            return

        api_url = resolve_api_url(project_dir)
        upload_batch(
            api_url,
            api_key,
            {
                "session_id": session_id,
                "source": "codex",
                "plugin_version": PLUGIN_VERSION,
                "hooks": entries,
            },
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
