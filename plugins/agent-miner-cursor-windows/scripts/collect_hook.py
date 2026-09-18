from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    SESSION_END_SLOT_WAIT_S,
    UPLOAD_OK,
    UPLOAD_RETRY_BUDGET_S,
    UPLOAD_TOO_LARGE,
    append_to_batch,
    append_to_batch_deduplicated,
    bootstrap_config,
    clear_batch,
    debug_log,
    drop_leading_entries,
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
    upload_slot,
    utcnow_iso,
)

# Upload on turn end (stop / sessionEnd) AND subagentStop: a Cursor subagent
# runs as its own conversation and its subagentStop can arrive after the parent
# turn's stop already uploaded, so we ship the captured subagent_transcript on
# subagentStop too. The cumulative batch + backend idempotency make the
# re-upload safe and link the child to the parent.
UPLOAD_HOOKS = {"stop", "sessionEnd", "subagentStop"}
GIT_BRANCH_HOOKS = {"sessionStart", "beforeSubmitPrompt"}
# Hooks whose payload may carry the turn's token counts. See where these are
# lifted into transcript_summary.api_calls for why both are listed.
TOKEN_BEARING_HOOKS = {"stop", "afterAgentResponse"}


def _thought_already_batched(records: list, payload: dict) -> bool:
    """True if this ``afterAgentThought`` duplicates one already in this turn.

    Cursor fires ``afterAgentThought`` more than once for a single thought: the
    copies carry identical ``text`` and ``duration_ms`` (sometimes with a
    suffixed ``generation_id`` such as ``…-0-r5vv``) and arrive microseconds
    apart, so the same thinking would otherwise be counted once per copy.

    Matches on ``(text, duration_ms)`` — which catches both the same-id and the
    suffixed-id copies — and scans back only to the current turn's
    ``beforeSubmitPrompt`` boundary. Genuinely distinct thoughts recur across
    turns (and legitimately share a ``generation_id`` within one), so the
    de-duplication must stay turn-scoped rather than span the whole session.

    Args:
        records: Envelopes already in this session's batch, oldest first. The
            scan walks them in reverse and stops at the current turn's opening
            ``beforeSubmitPrompt``.
        payload: The incoming ``afterAgentThought`` payload, compared on its
            ``text`` and ``duration_ms``.

    Returns:
        True when an identical thought is already recorded in this turn, so the
        caller drops the incoming copy. False otherwise — including when the
        payload carries no text, which is reported as NOT a duplicate and so is
        still appended. Do not "simplify" that branch to True: it would start
        discarding text-less thoughts, which is a behaviour change.
    """
    text = payload.get("text")
    if not text:
        return False
    duration = payload.get("duration_ms")
    for record in reversed(records):
        if record.get("hook_event_name") == "beforeSubmitPrompt":
            break
        if record.get("hook_event_name") == "afterAgentThought":
            prior = record.get("payload", {})
            if prior.get("text") == text and prior.get("duration_ms") == duration:
                return True
    return False


def _resolve_project_dir(payload: dict) -> str:
    """Return the project directory for *payload*.

    Prefers the first non-empty of: payload cwd, the Cursor/Claude project-dir
    env vars, then the first ``workspace_roots`` entry. Falls back to the
    process working directory when none is set.

    Args:
        payload: The hook payload, read for ``cwd`` and ``workspace_roots``.

    Returns:
        The resolved directory. In practice never '', since the final fallback
        is the process's own cwd — though ``os.getcwd()`` raises OSError if
        that directory has been unlinked.
    """
    candidates = [
        payload.get("cwd", ""),
        os.environ.get("CURSOR_PROJECT_DIR", ""),
        os.environ.get("CLAUDE_PROJECT_DIR", ""),
    ]
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        candidates.append(roots[0] if isinstance(roots[0], str) else "")
    for candidate in candidates:
        if candidate:
            return candidate
    return os.getcwd()


def _resolve_session_id(payload: dict) -> str:
    """Return the session identifier from *payload*, or '' if absent.

    Cursor sends ``conversation_id``; ``session_id`` is the claude_code
    fallback so a shared payload shape resolves under either runtime.

    Args:
        payload: Raw hook payload as delivered on stdin.

    Returns:
        The session identifier, or '' when the payload carries neither key.
    """
    return payload.get("conversation_id") or payload.get("session_id") or ""


def _speed_from_model_params(payload: dict) -> str:
    """Return the speed tier Cursor states on the payload, or "" when it does not.

    Cursor reports the session's mode choices as
    ``model_params: [{"id": "fast", "value": "false"}, ...]``. That is a statement
    where a trailing ``-fast`` in the model id is only an inference, so it is
    preferred when offered. Measured live, the field is present on some turns and
    null on others, so absence has to leave the id heuristic in charge rather than
    assert a default.

    ``model_params`` itself is documented, but ``fast`` is not among the ids the
    vendor names and no stability guarantee is published for these payloads, so
    its shape is not a contract: anything that is not the expected
    list-of-objects is treated as saying nothing. Guessing wrong here changes
    real cost, because Cursor publishes separate rates for its fast variants.

    Args:
        payload: The hook payload as the runtime sent it.

    Returns:
        "fast", "standard", or "" when the payload states nothing usable.
    """
    params = payload.get("model_params")
    if not isinstance(params, list):
        return ""
    for entry in params:
        if not isinstance(entry, dict) or entry.get("id") != "fast":
            continue
        value = entry.get("value")
        if isinstance(value, bool):
            return "fast" if value else "standard"
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes"):
                return "fast"
            if lowered in ("false", "0", "no"):
                return "standard"
        return ""
    return ""


# Ids Cursor uses for the reasoning-effort level in ``model_params``. The vendor
# documents the field as "Selected model parameters, such as thinking, context,
# or effort" and shows ``effort`` in its payload examples, so that spelling is
# from the contract. ``reasoning`` appears in no published schema — it is what
# the Kimi tiers were observed sending live — so it is accepted as a second
# spelling rather than assumed to be general.
# Source: https://cursor.com/docs/hooks (input schema shared by every hook).
EFFORT_PARAM_IDS = ("effort", "reasoning")


def _effort_from_model_params(payload: dict) -> str:
    """Return the reasoning-effort level Cursor states on the payload, or "".

    ``model_params`` is part of the documented input schema every hook shares:
    a list of ``{"id", "value"}`` items carrying the selected model parameters,
    ``effort`` among the ids the vendor names. It is read rather than inferred
    for that reason. See :data:`EFFORT_PARAM_IDS` for the second spelling,
    which is observed rather than documented.

    The trailing ``-high``/``-max`` on the model id restates the same choice,
    but only as an inference, and the API resolves that suffix to a price tier
    rather than to a level — so this is the only source ``AgentTurn.effort``
    has.

    The documented shape is honoured but not trusted: the vendor publishes no
    stability guarantee for these payloads, so anything that is not the
    expected list-of-objects is treated as saying nothing rather than guessed
    at. Measured live, the field is absent on some turns, and absence has to
    read as "no statement" rather than a default — asserting one would label
    every such turn with an effort it never ran at.

    Args:
        payload: The hook payload as the runtime sent it.

    Returns:
        The stated level, lowercased (for example "high" or "max"), or "" when
        the payload states nothing usable.
    """
    params = payload.get("model_params")
    if not isinstance(params, list):
        return ""
    for entry in params:
        if not isinstance(entry, dict) or entry.get("id") not in EFFORT_PARAM_IDS:
            continue
        value = entry.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        # Keep looking rather than give up on this id: the ids are alternative
        # spellings, and a payload carrying both must not lose the usable one
        # because the other happened to come first with an empty value.
        continue
    return ""


def main() -> None:
    """Process one hook invocation: batch the event and upload on turn end.

    Reads the hook event name from ``argv[1]`` and the JSON payload from stdin,
    appends an envelope to the session's batch file, and POSTs the accumulated
    batch to the Bloomfilter API on the ``stop`` / ``sessionEnd`` hooks.
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
    if is_foreign_runtime_payload(payload):
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

    session_id = _resolve_session_id(payload)
    if not session_id:
        debug_log(
            f"hook skipped: hook={hook_event_name} reason=no-session-id "
            f"(payload missing conversation_id/session_id)"
        )
        return

    # Cursor ships postToolUse tool_output as a JSON-encoded string; decode
    # so the API's extractor sees a dict, as it does for the other runtimes.
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
        # Expire batches whose session died before draining them. Swept here and
        # nowhere else: a directory scan costs about a millisecond, fine once per
        # session but not on a per-tool hook that fires thousands of times. A
        # batch is only removed once fully uploaded, so without this the
        # directory grows without limit.
        #
        # Deliberately BEFORE the key check. Batching happens whether or not a
        # key is configured, so gating the only garbage collector behind one
        # means an install without a key keeps every prompt, tool input and
        # tool output on disk in cleartext forever. Nothing recoverable is lost
        # by sweeping first: the age threshold is weeks, and a batch that old
        # belongs to a session that ended long ago.
        #
        # The session being started is passed so its own batch is never treated
        # as stale: at this point it has appended nothing, so a resumed
        # session's file still carries the previous sitting's mtime. Note this
        # runtime then starts the session from an empty batch anyway (below), so
        # here the argument only guarantees the sweep cannot race that reset.
        sweep_stale_batches(current_session_id=session_id)

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

    # Top-level cwd on sessionStart — the API's session config reads it from the
    # envelope rather than payload.workspace_roots (no list-index support).
    if hook_event_name == "sessionStart" and project_dir:
        envelope["cwd"] = project_dir

    # Synthesize transcript_summary.api_calls so the API's token path
    # (same as copilot/claude_code) sees the token data Cursor delivers directly
    # on the payload. Key rename: cursor's cache_write_tokens → the API's
    # cache_creation_tokens.
    #
    # Cursor documents no token fields on any hook, so where they arrive is a
    # measurement rather than a contract. Observed on 3.16.29: a completed
    # generation fires `afterAgentResponse` and `stop` with identical counts, and
    # an aborted one fires only `stop`, carrying no counts at all. Both events are
    # accepted here so a release that drops either still reports; today only
    # `afterAgentResponse` reaches a turn, because the collector API treats `stop`
    # as an ignored event.
    #
    # The fields stay optional: absent, the turn keeps no token data rather than
    # recording four zeroes, which would price a real turn at nothing. Values are
    # passed through as received — the API is responsible for coercing a count it
    # cannot use, since these fields are not typed by the vendor either.
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    if hook_event_name in TOKEN_BEARING_HOOKS and any(
        field in payload and payload.get(field) is not None for field in token_fields
    ):
        api_call = {
            "input_tokens": payload.get("input_tokens", 0),
            "output_tokens": payload.get("output_tokens", 0),
            "cache_read_tokens": payload.get("cache_read_tokens", 0),
            "cache_creation_tokens": payload.get("cache_write_tokens", 0),
            "model": payload.get("model", ""),
            "response_id": payload.get("generation_id", ""),
        }
        # Only set when the payload actually says: the API reads a missing key as
        # "no statement" and falls back to the trailing-suffix heuristic, while a
        # present key overrides it.
        stated_speed = _speed_from_model_params(payload)
        if stated_speed:
            api_call["speed"] = stated_speed
        stated_effort = _effort_from_model_params(payload)
        if stated_effort:
            api_call["effort"] = stated_effort
        envelope["transcript_summary"] = {"api_calls": [api_call]}

    # On subagentStop, parse the subagent's own transcript into a normalized
    # child conversation and attach it. Cursor fires this hook under the PARENT
    # conversation_id (so it lands in the parent batch) but leaves
    # agent_transcript_path null — the transcript lives at
    # <parent-conversation-dir>/subagents/<child-conversation>.jsonl, discovered
    # by matching the task. The backend turns subagent_transcript into a linked
    # child session keyed on payload.subagent_id. Read NOW — before the file is
    # garbage-collected. The subagent_id carries an embedded newline (the tool
    # call id plus a generation id); leave it as-is, it is stable across
    # start/stop so the backend's keying holds.
    if hook_event_name == "subagentStop":
        parent_transcript = payload.get("transcript_path") or os.environ.get(
            "CURSOR_TRANSCRIPT_PATH", ""
        )
        conversation = extract_subagent_conversation(
            parent_transcript,
            payload.get("task", ""),
        )
        if conversation:
            envelope["subagent_transcript"] = conversation

    if hook_event_name == "afterAgentThought":
        appended = append_to_batch_deduplicated(
            session_id,
            envelope,
            lambda records: _thought_already_batched(records, payload),
        )
        if not appended:
            debug_log(
                f"afterAgentThought skipped: reason=duplicate-in-turn "
                f"session_id={session_id}"
            )
    else:
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

        # One upload per session at a time. Three different hooks can trigger an
        # upload here, and the session-end one drains what it sent *by count* —
        # so two overlapping uploads would each snapshot the same records, each
        # send them, and the drain would then remove records that were never
        # sent. The slot also makes eviction and upload mutually exclusive, for
        # the same reason. Only the session-end hook waits for the slot: there
        # is no later hook to carry its records if it skips.
        slot_wait_seconds = (
            SESSION_END_SLOT_WAIT_S if hook_event_name == "sessionEnd" else 0.0
        )
        with upload_slot(session_id, wait_seconds=slot_wait_seconds) as has_slot:
            if not has_slot:
                debug_log(
                    f"upload skipped: hook={hook_event_name} "
                    f"session_id={session_id} reason=upload-already-in-flight"
                )
                return

            entries = read_batch(session_id)
            if not entries:
                debug_log(
                    f"upload skipped: hook={hook_event_name} "
                    f"session_id={session_id} reason=empty-batch"
                )
                return

            # Send only as much of the batch as fits in one request. Posting the
            # whole file made an oversize batch permanent: the server refuses it,
            # nothing is delivered, later hooks append, and the next attempt is
            # larger still. A prefix converts that into incremental delivery.
            upload_count = select_uploadable_prefix(entries)
            pending_entries = entries[:upload_count]

            batch_payload = {
                "session_id": session_id,
                "source": "cursor",
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
                    f"upload retry after too-large: hook={hook_event_name} "
                    f"session_id={session_id} hooks={len(pending_entries)}"
                )
                batch_payload["hooks"] = pending_entries
                upload_result = upload_batch(api_url, api_key, batch_payload)

            # A lone envelope the server will not accept can never be delivered,
            # and it sits at the head of the file — so keeping it blocks every
            # record behind it forever, which is the same permanent strand in a
            # new place. Drop exactly that one and let the rest through. Loud,
            # because it is real data loss: an envelope this large is usually one
            # enormous tool result, and the alternative is losing the whole
            # session's telemetry.
            if upload_result == UPLOAD_TOO_LARGE and len(pending_entries) == 1:
                # A batch line only has to be valid JSON to be read back, so a
                # truncated write can leave a bare scalar at the head. This is
                # the path that unblocks a stuck batch, so raising here would
                # both escape into the hook and leave the blocker in place —
                # the exact permanent strand it exists to prevent.
                head_entry = pending_entries[0]
                oversize_event = (
                    head_entry.get("hook_event_name", "?")
                    if isinstance(head_entry, dict)
                    else "?"
                )
                debug_log(
                    f"upload dropping undeliverable envelope: "
                    f"hook={hook_event_name} session_id={session_id} "
                    f"envelope={oversize_event} "
                    "reason=single-envelope-exceeds-server-limit"
                )
                drop_leading_entries(session_id, 1)
                return

            # Record how much of the batch the collector has now seen. A runtime
            # that re-sends everything uses this to know which records belong to
            # turns it has already closed, and so which are safe to shed when the
            # file grows.
            #
            # Inside the slot, not after it: the drain below removes by count,
            # and a pass that shifts the head between the snapshot and the drain
            # would redirect it onto records that were never sent.
            if upload_result == UPLOAD_OK:
                record_delivered_prefix(session_id, len(pending_entries))

                if hook_event_name == "sessionEnd":
                    # Remove only the entries we just uploaded; any hook appended
                    # concurrently during the upload is preserved for the next
                    # batch rather than truncated away.
                    drop_leading_entries(session_id, len(pending_entries))


if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        debug_log(
            f"collect_hook: unhandled exception type={type(exception).__name__} "
            f"message={exception!s}"
        )
        print(f"[bloomfilter] collect_hook failed: {exception}", file=sys.stderr)
    # Empty JSON on stdout — signals Cursor to proceed without modification.
    print("{}")
    sys.exit(0)
