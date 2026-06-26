import json
import os
import sys

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

UPLOAD_HOOKS = {"stop", "sessionEnd"}
GIT_BRANCH_HOOKS = {"sessionStart", "beforeSubmitPrompt"}


def _resolve_project_dir(payload):
    candidates = [
        payload.get("cwd", ""),
        os.environ.get("CURSOR_PROJECT_DIR", ""),
        os.environ.get("CLAUDE_PROJECT_DIR", ""),
    ]
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        candidates.append(roots[0] if isinstance(roots[0], str) else "")
    for c in candidates:
        if c:
            return c
    return os.getcwd()


def _resolve_session_id(payload):
    return payload.get("conversation_id") or payload.get("session_id") or ""


def main():
    hook_event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    if not hook_event_name:
        debug_log("hook skipped: reason=missing-hook-event-name (argv empty)")
        return

    payload = read_payload()
    if not isinstance(payload, dict):
        debug_log(
            f"hook skipped: hook={hook_event_name} reason=non-object-payload "
            f"type={type(payload).__name__}"
        )
        return
    session_id = _resolve_session_id(payload)
    if not session_id:
        debug_log(
            f"hook skipped: hook={hook_event_name} reason=no-session-id "
            f"(payload missing conversation_id/session_id)"
        )
        return

    # Cursor ships postToolUse tool_output as a JSON-encoded string; decode
    # so the BE extractor sees a dict (like claude_code / copilot).
    if hook_event_name in {"postToolUse", "postToolUseFailure"}:
        raw_output = payload.get("tool_output")
        if isinstance(raw_output, str) and raw_output.lstrip()[:1] in ("{", "["):
            try:
                payload["tool_output"] = json.loads(raw_output)
            except (json.JSONDecodeError, ValueError):
                pass

    project_dir = _resolve_project_dir(payload)
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if hook_event_name == "sessionStart":
        bootstrap_config(plugin_root)
        clear_batch(session_id)
        api_key = resolve_api_key()
        if not api_key:
            debug_log(
                f"hook skipped: hook=sessionStart session_id={session_id} "
                "reason=no-api-key (config.json missing api_key and "
                "BLOOMFILTER_API_KEY unset)"
            )
            return

    envelope = {
        "hook_event_name": hook_event_name,
        "received_at": utcnow_iso(),
        "plugin_version": PLUGIN_VERSION,
        "payload": payload,
    }

    if hook_event_name in GIT_BRANCH_HOOKS and project_dir:
        envelope["git_branch"] = get_git_branch(project_dir)

    # Top-level cwd on sessionStart — BE session config reads it from the
    # envelope rather than payload.workspace_roots (no list-index support).
    if hook_event_name == "sessionStart" and project_dir:
        envelope["cwd"] = project_dir

    # Synthesize transcript_summary.api_calls on the turn-end hook so the BE's
    # _apply_token_data path (same as copilot/claude_code) sees the token data
    # Cursor delivers directly on the payload. Key rename: cursor's
    # cache_write_tokens → BE's cache_creation_tokens.
    if hook_event_name == "afterAgentResponse" and (
        payload.get("input_tokens") or payload.get("output_tokens")
    ):
        envelope["transcript_summary"] = {
            "api_calls": [
                {
                    "input_tokens": payload.get("input_tokens", 0),
                    "output_tokens": payload.get("output_tokens", 0),
                    "cache_read_tokens": payload.get("cache_read_tokens", 0),
                    "cache_creation_tokens": payload.get("cache_write_tokens", 0),
                    "model": payload.get("model", ""),
                    "response_id": payload.get("generation_id", ""),
                }
            ]
        }

    append_to_batch(session_id, envelope)

    if hook_event_name in UPLOAD_HOOKS:
        api_key = resolve_api_key()
        if not api_key:
            debug_log(
                f"upload skipped: hook={hook_event_name} session_id={session_id} "
                "reason=no-api-key"
            )
            return

        api_url = resolve_api_url()
        entries = read_batch(session_id)
        if not entries:
            debug_log(
                f"upload skipped: hook={hook_event_name} session_id={session_id} "
                "reason=empty-batch"
            )
            return

        batch_payload = {
            "session_id": session_id,
            "source": "cursor",
            "plugin_version": PLUGIN_VERSION,
            "hooks": entries,
        }

        success = upload_batch(api_url, api_key, batch_payload)
        if success and hook_event_name == "sessionEnd":
            clear_batch(session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        debug_log(
            f"collect_hook: unhandled exception type={type(exc).__name__} "
            f"message={exc!s}"
        )
        print(f"[bloomfilter] collect_hook failed: {exc}", file=sys.stderr)
    # Empty JSON on stdout — signals Cursor to proceed without modification.
    print("{}")
    sys.exit(0)
