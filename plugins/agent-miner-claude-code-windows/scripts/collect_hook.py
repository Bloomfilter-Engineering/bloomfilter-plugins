#!/usr/bin/env python3
"""Universal hook handler for Bloomfilter agent mining.

Collects raw hook payloads, batches them in a JSONL file, and uploads
the batch to the Bloomfilter API on Stop and SessionEnd events.
"""

import os
import sys

# Ensure the scripts directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    append_to_batch,
    bootstrap_config,
    cleanup_session_batch,
    debug_log,
    drop_leading_entries,
    extract_subagent_conversation,
    extract_transcript_summary,
    get_git_branch,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    upload_batch,
    upload_slot,
    utcnow_iso,
)

# Hooks that trigger an upload to the BE
UPLOAD_HOOKS = {"Stop", "SessionEnd"}

# Hooks where we fetch the current git branch
GIT_BRANCH_HOOKS = {"SessionStart", "UserPromptSubmit"}

# Hooks where we extract transcript token summary
# Stop: current turn tokens; UserPromptSubmit: backfill previous turn if Stop missed tokens
TRANSCRIPT_HOOKS = {"Stop", "UserPromptSubmit"}


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
    session_id = payload.get("session_id", "")
    if not session_id:
        debug_log(f"hook skipped: hook={hook_event_name} reason=no-session-id")
        return

    project_dir = payload.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # On SessionStart: bootstrap config and check for API key
    if hook_event_name == "SessionStart":
        bootstrap_config(plugin_root)
        api_key = resolve_api_key()
        if not api_key:
            debug_log(
                f"hook skipped: hook=SessionStart session_id={session_id} "
                "reason=no-api-key (config.json missing api_key and "
                "BLOOMFILTER_API_KEY unset)"
            )
            return

    # Build the envelope — raw payload passed through untouched
    envelope = {
        "hook_event_name": hook_event_name,
        "received_at": utcnow_iso(),
        "plugin_version": PLUGIN_VERSION,
        "payload": payload,
    }

    # Fetch git branch only on specific hooks (avoid subprocess overhead)
    if hook_event_name in GIT_BRANCH_HOOKS and project_dir:
        envelope["git_branch"] = get_git_branch(project_dir)

    # Extract transcript token summary on Stop
    if hook_event_name in TRANSCRIPT_HOOKS:
        transcript_path = payload.get("transcript_path", "")
        summary = extract_transcript_summary(transcript_path)
        if summary:
            envelope["transcript_summary"] = summary

    # On SubagentStop, capture the subagent's own (sidechain) transcript so the
    # backend can build a full child AgentSession. Read it NOW — these files are
    # garbage-collected and may be gone by the time the batch uploads.
    if hook_event_name == "SubagentStop":
        agent_transcript_path = payload.get("agent_transcript_path", "")
        conversation = extract_subagent_conversation(
            agent_transcript_path,
            expected_last_message=payload.get("last_assistant_message"),
        )
        if conversation:
            envelope["subagent_transcript"] = conversation

    # Append to batch file
    append_to_batch(session_id, envelope)

    # Upload on Stop/SessionEnd
    if hook_event_name in UPLOAD_HOOKS:
        api_key = resolve_api_key()
        if not api_key:
            debug_log(
                f"upload skipped: hook={hook_event_name} session_id={session_id} "
                "reason=no-api-key"
            )
            return

        api_url = resolve_api_url()

        try:
            upload_and_drain(hook_event_name, session_id, api_url, api_key)
        finally:
            # SessionEnd is terminal — nothing can append or upload again, so
            # the drained batch file and the upload lock are removed instead of
            # lingering as zero-byte files, one pair per session. Runs outside
            # upload_slot so our own lock is released first, and on every exit
            # path (including the early returns inside upload_and_drain).
            if hook_event_name == "SessionEnd":
                cleanup_session_batch(session_id)


def upload_and_drain(hook_event_name, session_id, api_url, api_key):
    """Snapshot, POST, and drain the batch for *session_id*."""
    # Single-flight per session: the snapshot must be taken and drained
    # under the same guard, or two overlapping upload hooks would each
    # snapshot the same records and each drain them, deleting unsent
    # entries. See upload_slot's docstring.
    with upload_slot(session_id) as acquired:
        if not acquired:
            debug_log(
                f"upload skipped: hook={hook_event_name} "
                f"session_id={session_id} reason=upload-already-in-flight"
            )
            return

        entries = read_batch(session_id)
        if not entries:
            debug_log(
                f"upload skipped: hook={hook_event_name} session_id={session_id} "
                "reason=empty-batch"
            )
            return

        batch_payload = {
            "session_id": session_id,
            "source": "claude_code",
            "plugin_version": PLUGIN_VERSION,
            "hooks": entries,
        }

        success = upload_batch(api_url, api_key, batch_payload)
        if not success:
            return

        # Drain exactly the entries that were just uploaded, and only on
        # success — anything still in the file is retried by the next batch,
        # so a failed upload never loses data.
        #
        # Draining on every successful upload (not only on SessionEnd) is
        # what keeps Stop a roughly constant-cost hook. Stop fires at the
        # end of *every* turn, so retaining already-uploaded entries made
        # turn N re-POST turns 1..N: batches were observed reaching 9,243
        # entries / 41.9 MB and re-sent in full each turn, until the POST
        # outran the hook timeout and the runtime killed the process.
        #
        # Dropping by count rather than truncating: a hook from the next turn
        # may have appended while the POST was in flight, and those entries
        # land after the uploaded snapshot. Dropping by count preserves them;
        # a blanket truncate would discard them unsent.
        drop_leading_entries(session_id, len(entries))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            from bloomfilter_common import debug_log

            debug_log(
                f"collect_hook: unhandled exception type={type(exc).__name__} "
                f"message={exc!s}"
            )
        except Exception:
            pass  # Never block Claude
    # Always exit 0, unconditionally. This collector is pure telemetry and must
    # never influence the conversation. Per the hooks docs, exit 2 is a blocking
    # error whose effect is per-event, and on Stop it "prevents Claude from
    # stopping" — a non-zero exit here could therefore trap a session in a loop.
    # Any other non-zero exit surfaces a "hook error" notice to the user.
    sys.exit(0)
