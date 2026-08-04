#!/usr/bin/env python3
"""Universal hook handler for Bloomfilter agent mining.

Collects raw hook payloads, batches them in a JSONL file, and uploads
the batch to the Bloomfilter API on Stop and SessionEnd events.
"""

import os
import subprocess
import sys

# Ensure the scripts directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    SESSION_END_SLOT_WAIT_S,
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

# argv[1] sentinel marking a re-invocation of this script as the detached
# SessionEnd uploader (see _spawn_detached_upload). Chosen so it can never
# collide with a real hook event name.
DETACHED_UPLOAD_ARG = "__bloomfilter_detached_upload__"

# Hooks where we fetch the current git branch
GIT_BRANCH_HOOKS = {"SessionStart", "UserPromptSubmit"}

# Hooks where we extract transcript token summary
# Stop: current turn tokens; UserPromptSubmit: backfill previous turn if Stop missed tokens
TRANSCRIPT_HOOKS = {"Stop", "UserPromptSubmit"}


def main() -> None:
    """Handle one hook invocation: batch the payload, and upload when due.

    The hook event name arrives as ``argv[1]`` and the JSON payload on stdin.
    Every hook appends an envelope to the session's batch file; the events in
    :data:`UPLOAD_HOOKS` additionally ship the batch to the API. Any condition
    that makes the invocation unusable — no event name, a non-object payload, no
    session id, no API key — is recorded in the debug log and returns quietly,
    because a telemetry collector must never disturb the host.
    """
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
    plugin_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # On SessionStart: bootstrap config and check for API key
    if hook_event_name == "SessionStart":
        bootstrap_config(plugin_root_dir)
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
        transcript_summary = extract_transcript_summary(transcript_path)
        if transcript_summary:
            envelope["transcript_summary"] = transcript_summary

    # On SubagentStop, capture the subagent's own (sidechain) transcript so the
    # backend can build a full child AgentSession. Read it NOW — these files are
    # garbage-collected and may be gone by the time the batch uploads.
    if hook_event_name == "SubagentStop":
        agent_transcript_path = payload.get("agent_transcript_path", "")
        subagent_conversation = extract_subagent_conversation(
            agent_transcript_path,
            expected_last_message=payload.get("last_assistant_message"),
        )
        if subagent_conversation:
            envelope["subagent_transcript"] = subagent_conversation

    # Append to batch file
    append_to_batch(session_id, envelope)

    # Upload on Stop/SessionEnd.
    #
    # Stop uploads inline: it fires between turns, not on exit, so briefly
    # blocking on the POST is harmless and keeps the per-turn flow simple.
    #
    # SessionEnd hands the upload to a detached child instead. The POST is a
    # blocking network call, but the runtime cancels the SessionEnd hook the
    # instant the host begins shutting down — so an inline upload races that
    # teardown and gets killed mid-request ("Hook cancelled"), leaving the
    # final batch orphaned on disk. Detaching lets the host exit immediately
    # while the upload finishes independently in the background.
    if hook_event_name == "SessionEnd":
        if not _spawn_detached_upload(session_id):
            # Detach failed — upload inline rather than drop the batch. Blocking
            # briefly (and risking the cancel) beats losing the session's data.
            perform_upload(hook_event_name, session_id)
    elif hook_event_name in UPLOAD_HOOKS:
        perform_upload(hook_event_name, session_id)


def perform_upload(hook_event_name: str, session_id: str) -> None:
    """Resolve credentials and ship one session's batch, then clean up.

    Shared by the inline path (Stop, and the SessionEnd fallback when detaching
    fails) and by the detached SessionEnd child, so every path drives the
    identical upload → drain → cleanup sequence. Returns quietly when no API key
    is configured.

    Args:
        hook_event_name: Event that triggered the upload; selects the slot wait
            and whether the terminal SessionEnd cleanup runs.
        session_id: Session whose batch is uploaded.
    """
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
        # SessionEnd is terminal — nothing can append or upload again, so the
        # drained batch file and the upload lock are removed instead of
        # lingering as zero-byte files, one pair per session. Runs outside
        # upload_slot so our own lock is released first, and on every exit path
        # (including the early returns inside upload_and_drain).
        if hook_event_name == "SessionEnd":
            cleanup_session_batch(session_id)


def _spawn_detached_upload(session_id: str) -> bool:
    """Launch a detached child to upload *session_id*'s batch, then return at once.

    The child re-invokes this script with :data:`DETACHED_UPLOAD_ARG` and runs
    :func:`perform_upload` for SessionEnd. It is started in its own session /
    process group with its standard streams detached, so the host quitting — and
    the SIGHUP/SIGTERM that teardown delivers to the hook's process group —
    cannot reach it. The parent returns immediately, so the SessionEnd hook
    completes well inside its timeout and never blocks the host's exit on the
    network.

    The SessionEnd envelope is already appended to the batch before this is
    called, so the child's snapshot includes it.

    Args:
        session_id: Session whose batch the detached child uploads.

    Returns:
        True if the child was launched; False if spawning raised, so the caller
        can fall back to an inline upload rather than orphan the batch.
    """
    command = [
        sys.executable,
        os.path.abspath(__file__),
        DETACHED_UPLOAD_ARG,
        session_id,
    ]
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # Detach from the console and the parent's process group so closing the
        # host window does not take the uploader down with it.
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        # New session leader: detaches from the controlling terminal so the
        # host's shutdown signals to its own process group are not delivered.
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(command, **popen_kwargs)
        return True
    except Exception as exc:
        debug_log(
            f"detached upload spawn failed: session_id={session_id} "
            f"type={type(exc).__name__} message={exc!s}"
        )
        return False


def upload_and_drain(
    hook_event_name: str, session_id: str, api_url: str, api_key: str
) -> None:
    """Ship one session's batched records, then remove the ones that landed.

    Snapshot, POST and drain all happen inside a single :func:`upload_slot`, so
    two overlapping upload hooks cannot each snapshot the same records and each
    drain them — which would delete the second snapshot's records unsent.

    Returns without draining whenever the records are not confirmed stored: no
    upload slot, an empty batch, or any non-2xx response. Those records stay
    queued for the next batch, so telemetry is never dropped on failure.

    Args:
        hook_event_name: Event that triggered the upload. Selects how long to
            wait for the upload slot, and labels the debug log line.
        session_id: Session whose batch is uploaded.
        api_url: Base URL of the Bloomfilter API.
        api_key: Value sent as the ``X-MCP-Token`` header.
    """
    # Stop does not wait for the slot: if another upload holds it, this turn's
    # records ship with the next one. SessionEnd has no next turn, so it waits —
    # records skipped there would sit in the batch with nothing left to send
    # them. The wait is bounded so the hook still finishes inside its budget.
    slot_wait_seconds = (
        SESSION_END_SLOT_WAIT_S if hook_event_name == "SessionEnd" else 0.0
    )
    with upload_slot(session_id, wait_seconds=slot_wait_seconds) as has_upload_slot:
        if not has_upload_slot:
            debug_log(
                f"upload skipped: hook={hook_event_name} "
                f"session_id={session_id} reason=upload-already-in-flight "
                f"waited={slot_wait_seconds}s"
                + (
                    " WARNING=records-remain-unsent-no-later-hook-will-send-them"
                    if hook_event_name == "SessionEnd"
                    else ""
                )
            )
            return

        snapshot_entries = read_batch(session_id)
        if not snapshot_entries:
            debug_log(
                f"upload skipped: hook={hook_event_name} session_id={session_id} "
                "reason=empty-batch"
            )
            return

        batch_payload = {
            "session_id": session_id,
            "source": "claude_code",
            "plugin_version": PLUGIN_VERSION,
            "hooks": snapshot_entries,
        }

        upload_succeeded = upload_batch(api_url, api_key, batch_payload)
        if not upload_succeeded:
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
        drop_leading_entries(session_id, len(snapshot_entries))


if __name__ == "__main__":
    try:
        # Detached SessionEnd uploader: re-invoked by _spawn_detached_upload with
        # the sentinel + session id, it skips hook parsing (no stdin payload) and
        # just runs the upload. Any other invocation is a normal hook.
        if len(sys.argv) > 2 and sys.argv[1] == DETACHED_UPLOAD_ARG:
            perform_upload("SessionEnd", sys.argv[2])
        else:
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
