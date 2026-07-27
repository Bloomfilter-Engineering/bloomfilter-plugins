#!/usr/bin/env python3
"""Universal hook handler for Bloomfilter agent mining (VS Code Copilot).

Collects raw hook payloads, batches them in a JSONL file, and uploads
the batch to the Bloomfilter API on Stop events.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Ensure the scripts directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    _cap_text,
    append_to_batch,
    bootstrap_config,
    clear_batch,
    debug_log,
    derive_chat_sessions_path,
    detect_runtime,
    find_copilot_transcript,
    get_git_branch,
    normalize_hook_payload,
    parse_cli_transcript,
    parse_copilot_transcript,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    rewrite_batch,
    spawn_detached,
    upload_batch,
    utcnow_iso,
)

# Hooks that trigger an upload to the BE. SubagentStop is included because a
# subagent can finish after the parent turn's final Stop, which would otherwise
# strand its entry in the batch until the next turn. The batch is cumulative and
# the backend is idempotent, so the extra upload is safe (same rationale as the
# codex and cursor plugins).
UPLOAD_HOOKS = {"Stop", "SubagentStop"}

# Hooks where we fetch the current git branch
GIT_BRANCH_HOOKS = {"SessionStart", "UserPromptSubmit"}

# Hooks where we extract agent response and reasoning from the transcript
TRANSCRIPT_EXTRACT_HOOKS = {"Stop"}

# Hooks where we bootstrap config (SessionStart may not fire)
BOOTSTRAP_HOOKS = {"SessionStart", "UserPromptSubmit"}

# Background re-upload tuning. VS Code flushes the chatSessions token/model
# metadata ~10-22s after Stop (measured), so we poll with early-exit up to a
# generous cap. Because the worker is detached, this wait never blocks the hook.
REUPLOAD_POLL_INTERVAL = 1.5
REUPLOAD_MAX_WAIT = 90.0


def _chat_path_for_session(session_id: str, batch_entries: list[dict[str, Any]]) -> str:
    """Resolve the token/model-bearing chatSessions path for a session.

    Args:
        session_id: The hook session_id, used as a fallback search key.
        batch_entries: The session's batched hook envelopes; each entry's
            ``payload.transcript_path`` is inspected to derive a chatSessions
            path before falling back to a disk search.

    Returns:
        str: Path to the chatSessions ``.jsonl`` file, or '' if none found.
    """
    for entry in batch_entries:
        transcript_path = entry.get("payload", {}).get("transcript_path", "")
        chat_path = derive_chat_sessions_path(transcript_path)
        if chat_path:
            return chat_path
    return find_copilot_transcript(session_id, chat_sessions_only=True)


def _agent_id_from_tool_use_id(tool_use_id: str) -> str:
    """Return the subagent's agent_id for a runSubagent tool_use_id.

    VS Code suffixes the tool call id with ``__vscode-<n>`` while the
    SubagentStart/SubagentStop hooks carry the bare id as ``agent_id``, so the
    prefix is the join key between the two. Confirmed against a real capture
    from three independent places: the hook payload's ``agent_id``, the
    chatSessions response part's ``toolCallId``, and that part's enclosing
    ``toolCallRounds[].toolCalls[].id`` (the suffixed form).
    """
    return tool_use_id.split("__", 1)[0]


def _open_subagent(batch_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the runSubagent PreToolUse payload of an in-flight subagent.

    A subagent is in flight when the most recent subagent lifecycle event is a
    SubagentStart with no matching SubagentStop. Returns the payload of the
    ``runSubagent`` PreToolUse that spawned it, or None when no subagent is
    running (or its tool call can't be found).
    """
    agent_id = ""
    for entry in reversed(batch_entries):
        event = entry.get("hook_event_name")
        if event == "SubagentStop":
            return None  # most recent lifecycle event closed a subagent
        if event == "SubagentStart":
            agent_id = (entry.get("payload") or {}).get("agent_id", "")
            break
    if not agent_id:
        return None

    for entry in reversed(batch_entries):
        payload = entry.get("payload") or {}
        if (
            entry.get("hook_event_name") == "PreToolUse"
            and payload.get("tool_name") == "runSubagent"
            and _agent_id_from_tool_use_id(payload.get("tool_use_id", "")) == agent_id
        ):
            return payload
    return None


def _build_subagent_transcript(
    payload: dict[str, Any], batch_entries: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str, str]:
    """Build the child conversation for a completed runSubagent tool call.

    Called on the runSubagent PostToolUse — the first point where every piece
    exists. The prompt comes from this call's ``tool_input``, the response from
    its ``tool_response``, and the timing from the matching SubagentStart /
    SubagentStop already in the batch.

    ``model`` is left empty here: VS Code records the subagent's model (and
    cost) only in chatSessions, which flushes seconds later, so it is filled in
    by the re-upload worker via ``_overlay_chat_onto_subagents``.

    Returns ``(conversation, agent_id, agent_type)``; conversation is None when
    there is no matching subagent or no response to record.
    """
    agent_id = _agent_id_from_tool_use_id(payload.get("tool_use_id", ""))
    if not agent_id:
        return None, "", ""

    started_at = ended_at = agent_type = ""
    for entry in batch_entries:
        entry_payload = entry.get("payload") or {}
        if entry_payload.get("agent_id") != agent_id:
            continue
        event = entry.get("hook_event_name")
        if event == "SubagentStart":
            started_at = entry_payload.get("timestamp", "")
            agent_type = entry_payload.get("agent_type", "")
        elif event == "SubagentStop":
            ended_at = entry_payload.get("timestamp", "")
            agent_type = agent_type or entry_payload.get("agent_type", "")

    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt", "") if isinstance(tool_input, dict) else ""
    response = payload.get("tool_response") or ""
    if not isinstance(response, str):
        response = json.dumps(response)

    # No lifecycle events means this wasn't really a subagent run; no response
    # means there is nothing worth shipping. Returning None keeps an empty
    # {"turns": []} from passing the caller's truthiness check — the bug fixed
    # for Cursor in eecc793.
    if not started_at or not response.strip():
        return None, agent_id, agent_type

    return (
        {
            "turns": [
                {
                    "user_prompt": _cap_text(prompt),
                    "agent_response": _cap_text(response),
                    "model": "",
                    "response_id": "",
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "tool_calls": [],
                }
            ]
        },
        agent_id,
        agent_type,
    )


def _attach_to_subagent_stop(
    batch_entries: list[dict[str, Any]],
    agent_id: str,
    agent_type: str,
    conversation: dict[str, Any],
) -> bool:
    """Write a child conversation onto its SubagentStop entry, in place.

    The backend keys child sessions off the stop hook, so the transcript has to
    live there even though it can only be assembled once the spawning tool call
    returns. Mutates *batch_entries*; the caller persists with rewrite_batch.

    Returns True if a matching SubagentStop was found and updated.
    """
    for entry in reversed(batch_entries):
        if entry.get("hook_event_name") != "SubagentStop":
            continue
        if (entry.get("payload") or {}).get("agent_id") != agent_id:
            continue
        entry["subagent_transcript"] = conversation
        entry["agent_id"] = agent_id
        if agent_type:
            entry["agent_type"] = agent_type
        return True
    return False


def _overlay_chat_onto_subagents(
    batch_entries: list[dict[str, Any]], subagents: dict[str, Any]
) -> bool:
    """Fill each subagent turn's model and cost from the chatSessions records.

    The hook stream has neither; ``toolSpecificData.kind == "subagent"`` in
    chatSessions has both, keyed by the same agent_id. Returns True if any
    entry changed.
    """
    updated = False
    for entry in batch_entries:
        agent_id = entry.get("agent_id")
        record = subagents.get(agent_id) if agent_id else None
        if not record:
            continue
        turns = (entry.get("subagent_transcript") or {}).get("turns") or []
        for turn in turns:
            if record.get("model") and not turn.get("model"):
                turn["model"] = record["model"]
                updated = True
            if record.get("credits") is not None and turn.get("credits") is None:
                turn["credits"] = record["credits"]
                updated = True
    return updated


def _overlay_chat_onto_stops(
    batch_entries: list[dict[str, Any]], chat_requests: list[dict[str, Any]]
) -> bool:
    """Overlay exact response/tokens/model from chatSessions onto every Stop
    entry, matched in turn order (Nth Stop <-> Nth request record).

    Returns True if any entry changed.
    """
    updated = False
    rec_idx = 0
    for idx, entry in enumerate(batch_entries):
        if entry.get("hook_event_name") != "Stop":
            continue
        if rec_idx >= len(chat_requests):
            break
        rec = chat_requests[rec_idx]
        rec_idx += 1

        changed = False
        if rec.get("response_content") and not entry.get("agent_response"):
            entry["agent_response"] = rec["response_content"]
            changed = True
        if rec.get("reasoning_text") and not entry.get("reasoning_text"):
            entry["reasoning_text"] = rec["reasoning_text"]
            changed = True
        if rec.get("userMessage") and not entry.get("user_message"):
            entry["user_message"] = rec["userMessage"]
            changed = True
        if rec.get("input_tokens") or rec.get("output_tokens"):
            entry["transcript_summary"] = {
                "api_calls": [
                    {
                        "input_tokens": rec.get("input_tokens", 0),
                        "output_tokens": rec.get("output_tokens", 0),
                        "model": rec.get("resolvedModel") or rec.get("modelId", ""),
                        "request_id": rec.get("requestId", ""),
                        "response_id": rec.get("responseId", ""),
                    }
                ]
            }
            changed = True
        if changed:
            batch_entries[idx] = entry
            updated = True
    return updated


def run_reupload_worker(session_id: str) -> None:
    """Detached worker: wait for VS Code to flush the chatSessions token/model
    metadata, then overlay exact data onto every turn and re-upload.

    Runs in its own process (see spawn_detached) so it never blocks the Stop
    hook. The backend's update_or_create makes the re-upload idempotent, and
    exact token counts clear any earlier estimate.
    """
    api_key = resolve_api_key()
    if not api_key:
        debug_log(f"reupload_worker: aborted session_id={session_id} reason=no-api-key")
        return

    batch_entries = read_batch(session_id)
    n_stops = sum(1 for e in batch_entries if e.get("hook_event_name") == "Stop")
    if n_stops == 0:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} reason=no-stops "
            f"entries={len(batch_entries)}"
        )
        return

    # Poll until the last turn's tokens are flushed (early-exit), or until the
    # budget is exhausted.
    #
    # The chatSessions file is resolved INSIDE the loop, not before it: on the
    # first Stop of a new session VS Code has not created it yet (measured ~7 s
    # after the Stop hook fires), so resolving once up front and bailing on ""
    # made the worker abort before the very flush it exists to wait for — the
    # first turn of every session then shipped with estimated tokens and no
    # model. Waiting for the file to appear is the whole job.
    chat_path = ""
    chat_requests = []
    chat_subagents = {}
    waited = 0.0
    poll_count = 0
    while waited <= REUPLOAD_MAX_WAIT:
        if not chat_path:
            chat_path = _chat_path_for_session(session_id, batch_entries)
        if chat_path:
            parsed = parse_copilot_transcript(chat_path)
            chat_requests = parsed.get("requests", [])
            chat_subagents = parsed.get("subagents", {})
            poll_count += 1
            if len(chat_requests) >= n_stops:
                last = chat_requests[n_stops - 1]
                if last.get("input_tokens") or last.get("output_tokens"):
                    break
        time.sleep(REUPLOAD_POLL_INTERVAL)
        waited += REUPLOAD_POLL_INTERVAL
    else:
        debug_log(
            f"reupload_worker: budget-exhausted session_id={session_id} "
            f"polls={poll_count} waited={waited:.1f}s "
            f"chat_requests={len(chat_requests)} n_stops={n_stops} "
            f"chat_path={chat_path!r}"
        )

    if not chat_path:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} "
            f"reason=no-chat-sessions-path n_stops={n_stops} "
            f"waited={waited:.1f}s"
        )
        return

    if not chat_requests:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} "
            f"reason=no-chat-requests-parsed chat_path={chat_path!r}"
        )
        return

    # Re-read the batch (turns may have been appended while polling), overlay
    # exact data onto all Stop turns, persist, and re-upload the full session.
    batch_entries = read_batch(session_id)
    if not batch_entries:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} "
            "reason=batch-empty-after-poll"
        )
        return
    overlay_changed = _overlay_chat_onto_stops(batch_entries, chat_requests)
    # chatSessions is the only source of a subagent's model and cost, so the
    # same flush that carries the parent's tokens also completes its children.
    overlay_changed |= _overlay_chat_onto_subagents(batch_entries, chat_subagents)
    if overlay_changed:
        rewrite_batch(session_id, batch_entries)

    api_url = resolve_api_url()
    batch_payload = {
        "session_id": session_id,
        "source": "copilot",
        "plugin_version": PLUGIN_VERSION,
        "hooks": batch_entries,
    }
    upload_batch(api_url, api_key, batch_payload)


def main() -> None:
    # Detached background re-upload worker entrypoint.
    if len(sys.argv) > 1 and sys.argv[1] == "__reupload":
        session_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if session_id:
            run_reupload_worker(session_id)
        else:
            debug_log("reupload_worker: aborted reason=missing-session-id-argv")
        return

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
    # Copilot fires hooks in two payload conventions selected by event-name
    # casing (PascalCase->snake_case, camelCase->camelCase). Normalise so the
    # rest of the script reads uniformly — matters most for the CLI-only
    # camelCase ``subagentStart`` event.
    normalize_hook_payload(payload)
    session_id = payload.get("session_id", "")
    if not session_id:
        debug_log(f"hook skipped: hook={hook_event_name} reason=no-session-id")
        return

    runtime = detect_runtime(payload)

    # --- Subagent prompt suppression ------------------------------------
    # When a subagent runs, Copilot fires a UserPromptSubmit carrying the
    # SUBAGENT's prompt under the PARENT session_id, between SubagentStart and
    # SubagentStop, with nothing marking it as the child's. Recording it would
    # materialize a phantom user turn in the parent session.
    #
    # Two signals are required so a genuine prompt is never dropped: a subagent
    # must be in flight, AND the text must match the prompt that subagent was
    # spawned with.
    if hook_event_name == "UserPromptSubmit":
        spawning_call = _open_subagent(read_batch(session_id))
        if spawning_call:
            tool_input = spawning_call.get("tool_input")
            spawn_prompt = (
                tool_input.get("prompt", "") if isinstance(tool_input, dict) else ""
            )
            current_prompt = (payload.get("prompt") or "").strip()
            if current_prompt and current_prompt == spawn_prompt.strip():
                debug_log(
                    f"hook skipped: subagent UserPromptSubmit "
                    f"session_id={session_id} "
                    f"agent_id={_agent_id_from_tool_use_id(spawning_call.get('tool_use_id', ''))} "
                    "(subagent prompt, not the user's)"
                )
                return

    # --- Copilot CLI new-session duplicate-hook dedup -------------------
    # When a CLI session is started with an initial prompt, the CLI fires
    # `userPromptSubmitted` twice (once for the submission, once again after
    # the sessionStart hook completes) with identical prompt content, then
    # runs two model turns -> two `agentStop`s. Without dedup the backend
    # creates two turns for one user message. We:
    #   1. Skip the second UserPromptSubmit when it carries the same prompt
    #      as the immediately-previous UPS (no Stop between them).
    #   2. Replace the prior Stop when another Stop arrives with no UPS
    #      between them — the later Stop carries the user-visible response.
    if runtime == "copilot-cli" and hook_event_name == "UserPromptSubmit":
        current_prompt = (payload.get("prompt") or "").strip()
        if current_prompt:
            for prior in reversed(read_batch(session_id)):
                ev = prior.get("hook_event_name")
                if ev == "Stop":
                    break  # a Stop closed the prior turn; this UPS is new
                if ev == "UserPromptSubmit":
                    prior_prompt = (
                        (prior.get("payload") or {}).get("prompt") or ""
                    ).strip()
                    if prior_prompt == current_prompt:
                        debug_log(
                            f"hook skipped: duplicate UserPromptSubmit "
                            f"session_id={session_id} "
                            f"prompt={current_prompt[:60]!r} "
                            "(CLI new-session quirk)"
                        )
                        return
                    break

    if runtime == "copilot-cli" and hook_event_name == "Stop":
        existing = read_batch(session_id)
        last_idx = -1
        for i in range(len(existing) - 1, -1, -1):
            ev = existing[i].get("hook_event_name")
            if ev in ("UserPromptSubmit", "Stop"):
                last_idx = i
                break
        if last_idx >= 0 and existing[last_idx].get("hook_event_name") == "Stop":
            existing.pop(last_idx)
            rewrite_batch(session_id, existing)
            debug_log(
                f"replaced prior Stop session_id={session_id} "
                "(CLI new-session quirk: kept fresher response)"
            )

    project_dir = (
        payload.get("cwd", "")
        or os.environ.get("CLAUDE_PROJECT_DIR", "")
        or os.getcwd()
    )
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Bootstrap config on SessionStart or first UserPromptSubmit
    if hook_event_name in BOOTSTRAP_HOOKS:
        bootstrap_config(plugin_root)
        api_key = resolve_api_key()
        if not api_key:
            debug_log(
                f"hook skipped: hook={hook_event_name} session_id={session_id} "
                "reason=no-api-key (config.json missing api_key and "
                "BLOOMFILTER_API_KEY unset)"
            )
            return

    # Clear stale batch on SessionStart (new session = fresh batch)
    if hook_event_name == "SessionStart":
        clear_batch(session_id)

    # Build the envelope — raw payload passed through untouched
    envelope = {
        "hook_event_name": hook_event_name,
        "received_at": utcnow_iso(),
        "plugin_version": PLUGIN_VERSION,
        "runtime": runtime,
        "payload": payload,
    }

    # Fetch git branch only on specific hooks (avoid subprocess overhead)
    if hook_event_name in GIT_BRANCH_HOOKS and project_dir:
        envelope["git_branch"] = get_git_branch(project_dir)

    # Capture the child conversation for a completed subagent.
    #
    # The data only becomes complete at the runSubagent PostToolUse — the
    # subagent's answer is that call's tool_response, and SubagentStop precedes
    # it. But the backend reads subagent_transcript off the SubagentStop
    # envelope (_process_subagents iterates stop hooks), so we build it here and
    # write it back onto the SubagentStop entry already in the batch rather than
    # attaching it to this one. Attaching it here would create an empty child
    # session: the BE would find no transcript and fall through to its
    # last_assistant_message summary fallback, which Copilot never sends.
    if hook_event_name == "PostToolUse" and payload.get("tool_name") == "runSubagent":
        entries = read_batch(session_id)
        conversation, agent_id, agent_type = _build_subagent_transcript(
            payload, entries
        )
        if conversation and _attach_to_subagent_stop(
            entries, agent_id, agent_type, conversation
        ):
            rewrite_batch(session_id, entries)

    # Extract agent response, reasoning, and token data from the Copilot
    # transcript.  The transcript is written asynchronously so we retry
    # briefly until the response content is available.
    if hook_event_name in TRANSCRIPT_EXTRACT_HOOKS:
        # Count expected turns so we don't break on stale data from a
        # previous turn whose transcript entry hasn't been superseded yet.
        batch_entries = read_batch(session_id)
        expected_turns = sum(
            1 for e in batch_entries if e.get("hook_event_name") == "UserPromptSubmit"
        )

        # Two runtimes, two transcript layouts:
        #
        # copilot-vscode  - payload.transcript_path points at the OLD format
        #   (GitHub.copilot-chat/transcripts/<uuid>.jsonl). It has messages
        #   and reasoning but NO tokens/model. Exact tokens+model live in
        #   chatSessions/<uuid>.jsonl, which VS Code flushes ~10-22 s after
        #   Stop. Two-phase parse + chatSessions overlay; the detached
        #   re-upload worker below handles the last-turn flush gap.
        #
        # copilot-cli     - payload.transcript_path points at
        #   ~/.copilot/session-state/<id>/events.jsonl, which is written
        #   synchronously and carries model, outputTokens, response content,
        #   and reasoning all at once. No async overlay needed; the CLI does
        #   not expose input tokens so those stay 0 and will be estimated.

        payload_path = payload.get("transcript_path", "")

        if runtime == "copilot-cli":
            parsed = parse_cli_transcript(payload_path) if payload_path else None
            requests = parsed.get("requests", []) if parsed else []
            current_req = (
                requests[-1] if requests and len(requests) >= expected_turns else None
            )
            have_current_turn = current_req is not None and (
                current_req.get("response_content") or current_req.get("output_tokens")
            )
            # CLI has no separate chatSessions file — the same parsed feed
            # is the authoritative source for the earlier-turn backfill.
            chat_requests = requests
        else:
            # --- Phase 1: old transcript for messages (single parse) ---
            # No retry-wait: the background re-upload worker below polls
            # chatSessions and overlays any missing response_content /
            # reasoning_text onto every Stop entry idempotently within
            # ~10-22 s, so blocking the Stop hook here is wasteful.
            parsed = parse_copilot_transcript(payload_path) if payload_path else None
            requests = parsed.get("requests", []) if parsed else []
            have_current_turn = len(requests) >= expected_turns and requests[-1].get(
                "response_content"
            )
            current_req = (
                requests[-1] if requests and len(requests) >= expected_turns else None
            )

            # --- Phase 2: chatSessions for tokens/model/IDs (best effort) ---
            chat_path = (
                derive_chat_sessions_path(payload_path)
                or find_copilot_transcript(session_id, chat_sessions_only=True)
                or ""
            )
            chat_requests = []
            if chat_path:
                chat_parsed = parse_copilot_transcript(chat_path)
                chat_requests = chat_parsed.get("requests", [])

            # Overlay token/model/ID data from chatSessions onto current
            # turn when available.
            if current_req:
                turn_idx = len(requests) - 1
                if turn_idx < len(chat_requests):
                    chat_req = chat_requests[turn_idx]
                    if chat_req.get("input_tokens") or chat_req.get("output_tokens"):
                        current_req["input_tokens"] = chat_req["input_tokens"]
                        current_req["output_tokens"] = chat_req["output_tokens"]
                    if chat_req.get("resolvedModel"):
                        current_req["resolvedModel"] = chat_req["resolvedModel"]
                    if chat_req.get("requestId"):
                        current_req["requestId"] = chat_req["requestId"]
                    if chat_req.get("responseId"):
                        current_req["responseId"] = chat_req["responseId"]
                    # Prefer chatSessions reasoning_parts (has thinking_id
                    # and timestamps from toolCallRounds).
                    if chat_req.get("reasoning_parts"):
                        current_req["reasoning_parts"] = chat_req["reasoning_parts"]
                        if chat_req.get("reasoning_text"):
                            current_req["reasoning_text"] = chat_req["reasoning_text"]

        # Build envelope fields from the combined data.
        if current_req:
            if current_req.get("response_content"):
                envelope["agent_response"] = current_req["response_content"]
            if current_req.get("reasoning_text"):
                envelope["reasoning_text"] = current_req["reasoning_text"]
            if current_req.get("userMessage"):
                envelope["user_message"] = current_req["userMessage"]

            envelope["transcript_summary"] = {
                "api_calls": [
                    {
                        "input_tokens": current_req.get("input_tokens", 0),
                        "output_tokens": current_req.get("output_tokens", 0),
                        "model": (
                            current_req.get("resolvedModel")
                            or current_req.get("modelId", "")
                        ),
                        "request_id": current_req.get("requestId", ""),
                        "response_id": current_req.get("responseId", ""),
                    }
                ]
            }

        # Backfill earlier Stop entries that are missing agent_response
        # OR have 0 token counts.  Use chatSessions data when available
        # — it has token counts for previous turns even though the
        # current turn's data isn't ready yet.  Match by turn order:
        # the Nth Stop entry corresponds to the Nth request record.
        backfill_source = chat_requests if chat_requests else requests
        if len(backfill_source) > 1:
            earlier = backfill_source[:-1] if have_current_turn else backfill_source
            # Walk Stop entries and transcript records in lockstep so
            # the Nth Stop always matches the Nth record — even when
            # some Stops already have tokens from a prior backfill.
            rec_idx = 0
            updated = False
            for idx, e in enumerate(batch_entries):
                if e.get("hook_event_name") != "Stop":
                    continue
                if rec_idx >= len(earlier):
                    break
                rec = earlier[rec_idx]
                rec_idx += 1

                # Check if this entry needs backfill
                summary = e.get("transcript_summary", {})
                calls = summary.get("api_calls", [{}])
                has_tokens = any(
                    c.get("input_tokens") or c.get("output_tokens") for c in calls
                )
                if e.get("agent_response") and has_tokens:
                    continue  # already complete

                if rec.get("response_content") and not e.get("agent_response"):
                    e["agent_response"] = rec["response_content"]
                if rec.get("reasoning_text") and not e.get("reasoning_text"):
                    e["reasoning_text"] = rec["reasoning_text"]
                if rec.get("userMessage") and not e.get("user_message"):
                    e["user_message"] = rec["userMessage"]
                if rec.get("input_tokens") or rec.get("output_tokens"):
                    e["transcript_summary"] = {
                        "api_calls": [
                            {
                                "input_tokens": rec.get("input_tokens", 0),
                                "output_tokens": rec.get("output_tokens", 0),
                                "model": (
                                    rec.get("resolvedModel") or rec.get("modelId", "")
                                ),
                                "request_id": rec.get("requestId", ""),
                                "response_id": rec.get("responseId", ""),
                            }
                        ]
                    }
                batch_entries[idx] = e
                updated = True
            if updated:
                rewrite_batch(session_id, batch_entries)

        # Inject synthetic Thinking hooks for the current turn only.
        if current_req:
            for part in current_req.get("reasoning_parts", []):
                ts = part.get("timestamp", 0)
                thinking_hook = {
                    "hook_event_name": "Thinking",
                    "received_at": (
                        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                        if isinstance(ts, (int, float)) and ts
                        else envelope["received_at"]
                    ),
                    "plugin_version": PLUGIN_VERSION,
                    "payload": {
                        "session_id": session_id,
                        "content": part.get("content", ""),
                        "thinking_id": part.get("thinking_id", ""),
                        "request_id": current_req.get("requestId", ""),
                    },
                }
                append_to_batch(session_id, thinking_hook)

    # Append to batch file
    append_to_batch(session_id, envelope)

    # Upload on Stop — batch is NOT cleared so it accumulates the full
    # session history.  The backend's update_or_create handles idempotency
    # for existing turns.  Earlier Stop entries that were missing their
    # agent_response (transcript not flushed in time) are backfilled above
    # from the now-complete transcript.
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
            "source": "copilot",
            "plugin_version": PLUGIN_VERSION,
            "hooks": entries,
        }

        upload_batch(api_url, api_key, batch_payload)

        # On VS Code, chatSessions metadata is flushed ~10-22 s after Stop,
        # so if this turn shipped without exact tokens we hand off to a
        # detached worker that polls for the flush and re-uploads. The CLI
        # writes events.jsonl synchronously — no flush to wait out, so the
        # worker is skipped there.
        #
        # A subagent turn awaiting its model/cost also needs the flush, and a
        # turn can carry exact tokens while its children are still bare — so
        # check for that independently rather than only on missing tokens.
        summary = envelope.get("transcript_summary", {})
        calls = summary.get("api_calls", [{}])
        has_tokens = any(c.get("input_tokens") or c.get("output_tokens") for c in calls)
        needs_subagent_data = any(
            not turn.get("model")
            for e in entries
            for turn in (e.get("subagent_transcript") or {}).get("turns") or []
        )
        # The worker matches chatSessions requests to Stop entries, so it has
        # nothing to do until the turn has actually stopped. Without this an
        # upload triggered by SubagentStop — which precedes the parent's Stop —
        # spawns a process that immediately aborts with reason=no-stops.
        has_stop = any(e.get("hook_event_name") == "Stop" for e in entries)
        if (
            has_stop
            and (not has_tokens or needs_subagent_data)
            and runtime == "copilot-vscode"
        ):
            python_exe = sys.executable or "python3"
            spawned = spawn_detached(
                [python_exe, os.path.abspath(__file__), "__reupload", session_id]
            )
            if not spawned:
                debug_log(
                    f"reupload_worker: spawn failed session_id={session_id} "
                    "(exact token counts will not be backfilled)"
                )


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
            pass  # Never block Copilot
        print(f"[bloomfilter] collect_hook failed: {exc}", file=sys.stderr)
    sys.exit(0)
