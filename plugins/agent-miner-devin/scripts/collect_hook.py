#!/usr/bin/env python3

import os
import subprocess
import sys
import time

# Ensure the scripts directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    SESSION_END_DRAIN_BUDGET_S,
    SESSION_END_SLOT_WAIT_S,
    UPLOAD_OK,
    UPLOAD_RETRY_BUDGET_S,
    UPLOAD_TOO_LARGE,
    append_to_batch,
    bootstrap_config,
    cleanup_session_batch,
    debug_log,
    drop_leading_entries,
    get_git_branch,
    is_foreign_runtime_payload,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    select_uploadable_prefix,
    sweep_stale_batches,
    upload_batch,
    upload_slot,
    utcnow_iso,
)
from devin_tool_pairing import (
    claim_tool_call_id,
    clear_tool_pairing,
    resolve_tool_call_id,
    sweep_stale_tool_state,
)
from devin_transcript import (
    BACKFILL_MAX_BYTES,
    TRANSCRIPT_MAX_BYTES,
    session_metadata,
    summarize_session_turn,
)

# The `source` value the API files these sessions under. Must match a
# configs/<source>.json on the server.
SOURCE = "devin"

# Every lifecycle event Devin CLI documents. Anything else is still batched —
# the API ignores hook names it has no mapping for — but is logged so a new
# event shows up in debug.log rather than vanishing.
SUPPORTED_HOOKS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "PostCompaction",
    "SessionEnd",
}

# Hooks that trigger an upload to the API
UPLOAD_HOOKS = {"Stop", "SessionEnd"}

# argv[1] sentinel marking a re-invocation of this script as the detached
# SessionEnd uploader (see _spawn_detached_upload). Chosen so it can never
# collide with a real hook event name.
DETACHED_UPLOAD_ARG = "__bloomfilter_detached_upload__"

# Hooks where we fetch the current git branch
GIT_BRANCH_HOOKS = {"SessionStart", "UserPromptSubmit"}

# Hooks where the transcript's trailing turn is summarized. Stop is the turn
# that just finished. UserPromptSubmit is the recovery path: if Devin had not
# flushed the transcript when Stop fired, the previous turn's usage is still
# the trailing turn now, and the API backfills a zero-token turn from it.
TRANSCRIPT_HOOKS = {"Stop", "UserPromptSubmit"}

# The tool hooks that share a synthesized tool_call_id (see devin_tool_pairing).
TOOL_START_HOOK = "PreToolUse"
TOOL_END_HOOK = "PostToolUse"

# Envelope name given to a PostToolUse whose tool_response reports failure. The
# API's pairing config resolves status from the END hook's name, not from a
# payload value, so a failed call has to arrive under a distinct name to be
# stored as TOOL_ERROR — the same split Claude Code makes natively.
TOOL_FAILURE_HOOK = "PostToolUseFailure"

# Tools whose start hook is annotated with whether the target file already
# exists. Devin's `write` replaces a whole file and its response is free text,
# so this is the only point at which create can be told from overwrite.
FILE_EXISTENCE_TOOLS = {"write"}

# Payload keys Devin never sends. Claude Code and Codex put these in every
# payload, so their presence means another runtime's hook reached this
# collector (Devin loads Claude-format hook files, and the reverse can happen
# via a shared marketplace). Filing such a session as Devin would misattribute
# another tool's work, so the payload is refused.
NON_DEVIN_PAYLOAD_KEYS = ("transcript_path",)


def _resolve_project_dir(payload: dict) -> str:
    """Find the project root for this hook invocation.

    Devin CLI's payloads carry no ``cwd``. The documented source is the
    ``DEVIN_PROJECT_DIR`` environment variable, which "always points at the
    hook's project root" even when the hook runs from a different working
    directory; ``CLAUDE_PROJECT_DIR`` is set alongside it for Claude-plugin
    compatibility. Payload keys are accepted in case a later release adds them,
    and the process cwd is the last resort.

    Args:
        payload: The decoded hook payload.

    Returns:
        An absolute path, or an empty string when nothing resolves.
    """
    for candidate in (
        os.environ.get("DEVIN_PROJECT_DIR", ""),
        os.environ.get("CLAUDE_PROJECT_DIR", ""),
        payload.get("cwd", ""),
        payload.get("project_dir", ""),
    ):
        if isinstance(candidate, str) and candidate and os.path.isabs(candidate):
            return candidate
    try:
        return os.getcwd()
    except OSError:
        return ""


def _annotate_file_existence(tool_input: dict) -> None:
    """Record whether a whole-file write targets an existing file.

    Stored inside ``tool_input`` under a namespaced key so the API's file-edit
    extractor, which sees only ``tool_input``/``tool_output``, can classify the
    edit as CREATE or MODIFY. A stat only — nothing is read or written.

    Args:
        tool_input: The payload's ``tool_input`` dict, mutated in place.
    """
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return
    try:
        tool_input["bloomfilter_file_existed"] = os.path.exists(file_path)
    except (OSError, ValueError):
        return


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

    # Refuse a session that belongs to a different runtime. Editors discover and
    # execute each other's collectors, so this one can be handed hooks from a
    # session it does not serve — and whichever collector uploads first is the
    # one the whole session gets filed under, so acting on it silently records
    # another tool's work as this one's.
    if is_foreign_runtime_payload(payload) or (
        isinstance(payload, dict)
        and any(key in payload for key in NON_DEVIN_PAYLOAD_KEYS)
    ):
        debug_log(
            f"hook skipped: hook={hook_event_name} "
            "reason=payload-belongs-to-another-runtime"
        )
        return
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

    if hook_event_name not in SUPPORTED_HOOKS:
        debug_log(
            f"hook recorded but unmapped: hook={hook_event_name} "
            f"session_id={session_id} reason=not-a-documented-devin-event"
        )

    project_dir = _resolve_project_dir(payload)
    plugin_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # On SessionStart: bootstrap config and check for API key
    if hook_event_name == "SessionStart":
        bootstrap_config(plugin_root_dir)
        # Expire batches whose session died before draining them. Swept here and
        # nowhere else: a directory scan costs about a millisecond, fine once per
        # session but not on a per-tool hook that fires thousands of times. A
        # batch is only removed once fully uploaded, so without this the
        # directory grows without limit.
        #
        # Deliberately BEFORE the key check. Batching happens whether or not a
        # key is configured, so gating the only garbage collector behind one
        # means an install without a key keeps every prompt, tool input and
        # tool output on disk in cleartext forever.
        #
        # The session being started is passed so its own batch is spared — at
        # this point it has appended nothing, so a resumed session's file still
        # carries the previous sitting's mtime and would look stale.
        sweep_stale_batches(current_session_id=session_id)
        sweep_stale_tool_state()

        api_key = resolve_api_key()
        if not api_key:
            debug_log(
                f"hook skipped: hook=SessionStart session_id={session_id} "
                "reason=no-api-key (config.json missing api_key and "
                "BLOOMFILTER_API_KEY unset)"
            )
            return

    # Build the envelope — raw payload passed through untouched apart from the
    # synthesized tool_call_id below, which Devin does not supply.
    envelope = {
        "hook_event_name": hook_event_name,
        "received_at": utcnow_iso(),
        "plugin_version": PLUGIN_VERSION,
        "payload": payload,
    }

    # The API reads working_directory from the envelope root for this runtime.
    # Stamped on every envelope, not only SessionStart: a plugin installed
    # mid-session, or a cloud session where SessionStart never fires, creates
    # the session lazily from whatever hook arrives first.
    if project_dir:
        envelope["cwd"] = project_dir

    # Fetch git branch only on specific hooks (avoid subprocess overhead)
    if hook_event_name in GIT_BRANCH_HOOKS and project_dir:
        envelope["git_branch"] = get_git_branch(project_dir)

    # Session-level identity lives in the transcript root, not the payload. The
    # transcript usually does not exist yet at SessionStart, so this is
    # best-effort; the per-turn model still arrives via the Stop summary.
    if hook_event_name == "SessionStart":
        for metadata_key, metadata_value in session_metadata(session_id).items():
            payload.setdefault(metadata_key, metadata_value)

    # Devin's tool hooks carry no per-call id. Mint one on the start hook and
    # hand the same one to the matching end hook so the API can pair them.
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input")
    if hook_event_name == TOOL_START_HOOK and "tool_call_id" not in payload:
        payload["tool_call_id"] = claim_tool_call_id(session_id, tool_name, tool_input)
        if tool_name in FILE_EXISTENCE_TOOLS and isinstance(tool_input, dict):
            _annotate_file_existence(tool_input)
    elif hook_event_name == TOOL_END_HOOK:
        if "tool_call_id" not in payload:
            payload["tool_call_id"], _paired = resolve_tool_call_id(
                session_id, tool_name, tool_input
            )
        tool_response = payload.get("tool_response")
        if isinstance(tool_response, dict) and tool_response.get("success") is False:
            envelope["hook_event_name"] = TOOL_FAILURE_HOOK

    # Summarize the transcript's trailing turn: tokens, model, request ids,
    # time to first token and ACU cost, none of which are in the hook payload.
    # The read is capped to the budget of the hook doing it, and runs before
    # the append so the summary rides on the envelope the API keys on.
    if hook_event_name in TRANSCRIPT_HOOKS:
        max_bytes = (
            TRANSCRIPT_MAX_BYTES if hook_event_name == "Stop" else BACKFILL_MAX_BYTES
        )
        summary = summarize_session_turn(session_id, max_bytes)
        if summary:
            agent_response = summary.pop("agent_response", "")
            if summary.get("api_calls") or hook_event_name == "Stop":
                envelope["transcript_summary"] = summary
            if hook_event_name == "Stop":
                # Prefer the runtime's own final message; the transcript's last
                # agent message is the fallback when the payload lacks it.
                envelope["agent_response"] = (
                    payload.get("last_assistant_message") or agent_response
                )
    if hook_event_name == "Stop":
        envelope.setdefault("agent_response", payload.get("last_assistant_message", ""))
        # Every tool of the turn has ended; anything still parked is abandoned.
        clear_tool_pairing(session_id)

    if hook_event_name == "SessionEnd":
        clear_tool_pairing(session_id)

    # Append to batch file
    append_to_batch(session_id, envelope)

    # Upload on Stop/SessionEnd.
    #
    # Stop uploads inline: it fires between turns, not on exit, so briefly
    # blocking on the POST is harmless and keeps the per-turn flow simple.
    #
    # SessionEnd hands the upload to a detached child instead. The POST is a
    # blocking network call, and a host tearing down can cancel the hook
    # mid-request, leaving the final batch orphaned on disk. Detaching lets the
    # host exit immediately while the upload finishes independently.
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
        if hook_event_name == "SessionEnd":
            # Terminal hook: nothing later will ship what one request leaves
            # behind, and a turn whose own envelopes exceed a request forces a
            # partial cut — so keep draining until the batch is empty. Safe to
            # loop here and nowhere else: this path runs detached, outside the
            # runtime's hook timeout. Bounded by wall clock, and stopped the
            # moment a pass delivers nothing so a persistent failure cannot spin.
            drain_deadline = time.monotonic() + SESSION_END_DRAIN_BUDGET_S
            while time.monotonic() < drain_deadline:
                remaining_before = len(read_batch(session_id))
                if not remaining_before:
                    break
                upload_and_drain(hook_event_name, session_id, api_url, api_key)
                if len(read_batch(session_id)) >= remaining_before:
                    break
        else:
            upload_and_drain(hook_event_name, session_id, api_url, api_key)
    finally:
        # SessionEnd is terminal — nothing can append or upload again, so the
        # drained batch file and the upload lock are removed instead of
        # lingering as zero-byte files, one pair per session.
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
    except Exception as exception:
        debug_log(
            f"detached upload spawn failed: session_id={session_id} "
            f"type={type(exception).__name__} message={exception!s}"
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

        # Send only as much of the batch as fits in one request. Posting the
        # whole file made an oversize batch permanent: the server answers 413,
        # nothing drains, later hooks append, and the next attempt is larger
        # still. A prefix converts that into incremental delivery.
        upload_count = select_uploadable_prefix(snapshot_entries)
        pending_entries = snapshot_entries[:upload_count]

        batch_payload = {
            "session_id": session_id,
            "source": SOURCE,
            "plugin_version": PLUGIN_VERSION,
            "hooks": pending_entries,
        }

        # Budget starts before the first request, not after it: the first
        # request can burn the whole socket timeout on its own, so a clock
        # started afterwards lets the worst case run past the hook's limit
        # and be killed mid-flight — the failure this budget exists to stop.
        retry_deadline = time.monotonic() + UPLOAD_RETRY_BUDGET_S
        upload_result = upload_batch(api_url, api_key, batch_payload)

        while (
            upload_result == UPLOAD_TOO_LARGE
            and len(pending_entries) > 1
            and time.monotonic() < retry_deadline
        ):
            pending_entries = pending_entries[: len(pending_entries) // 2]
            debug_log(
                f"upload retry after 413: hook={hook_event_name} "
                f"session_id={session_id} hooks={len(pending_entries)}"
            )
            batch_payload["hooks"] = pending_entries
            upload_result = upload_batch(api_url, api_key, batch_payload)

        # A lone envelope the server will not accept can never be delivered, and
        # it sits at the head of the file — so keeping it blocks every record
        # behind it forever. Drop exactly that one and let the rest through.
        # Loud, because it is real data loss.
        if upload_result == UPLOAD_TOO_LARGE and len(pending_entries) == 1:
            head_entry = pending_entries[0]
            oversize_event = (
                head_entry.get("hook_event_name", "?")
                if isinstance(head_entry, dict)
                else "?"
            )
            debug_log(
                f"upload dropping undeliverable envelope: hook={hook_event_name} "
                f"session_id={session_id} envelope={oversize_event} "
                "reason=single-envelope-exceeds-server-limit"
            )
            drop_leading_entries(session_id, 1)
            return

        if upload_result != UPLOAD_OK:
            return

        # Drain exactly the entries that were just uploaded, and only on
        # success — anything still in the file is retried by the next batch,
        # so a failed upload never loses data. Dropping by count rather than
        # truncating preserves entries a concurrent hook appended mid-POST.
        drop_leading_entries(session_id, len(pending_entries))


if __name__ == "__main__":
    try:
        # Detached SessionEnd uploader: re-invoked by _spawn_detached_upload with
        # the sentinel + session id, it skips hook parsing (no stdin payload) and
        # just runs the upload. Any other invocation is a normal hook.
        if len(sys.argv) > 2 and sys.argv[1] == DETACHED_UPLOAD_ARG:
            perform_upload("SessionEnd", sys.argv[2])
        else:
            main()
    except Exception as exception:
        try:
            debug_log(
                f"collect_hook: unhandled exception type={type(exception).__name__} "
                f"message={exception!s}"
            )
        except Exception:
            pass  # Never block Devin
    # Always exit 0, unconditionally. This collector is pure telemetry and must
    # never influence the conversation. Devin's hook contract treats exit 2 as
    # "block the action" — on Stop that traps the agent in a loop, on
    # UserPromptSubmit it discards the user's prompt — and any other non-zero
    # exit surfaces a hook error to the user.
    sys.exit(0)
