#!/usr/bin/env python3
"""Universal hook handler for Bloomfilter agent mining (VS Code Copilot).

Collects raw hook payloads, batches them in a JSONL file, and uploads
the batch to the Bloomfilter API on Stop events.
"""

import os
import sys
import time
from datetime import datetime, timezone

# Ensure the scripts directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
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

# Hooks that trigger an upload to the BE
UPLOAD_HOOKS = {"Stop"}

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


def _chat_path_for_session(session_id, batch_entries):
    """Resolve the chatSessions path for a session from its batch entries."""
    for entry in batch_entries:
        transcript_path = entry.get("payload", {}).get("transcript_path", "")
        chat_path = derive_chat_sessions_path(transcript_path)
        if chat_path:
            return chat_path
    return find_copilot_transcript(session_id)


def _overlay_chat_onto_stops(batch_entries, chat_requests):
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


def run_reupload_worker(session_id):
    """Detached worker: wait for VS Code to flush the chatSessions token/model
    metadata, then overlay exact data onto every turn and re-upload.

    Runs in its own process (see spawn_detached) so it never blocks the Stop
    hook. The backend's update_or_create makes the re-upload idempotent, and
    exact token counts clear any earlier estimate.
    """
    debug_log(f"reupload_worker: started session_id={session_id} pid={os.getpid()}")

    api_key = resolve_api_key()
    if not api_key:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} reason=no-api-key"
        )
        return

    batch_entries = read_batch(session_id)
    n_stops = sum(1 for e in batch_entries if e.get("hook_event_name") == "Stop")
    if n_stops == 0:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} reason=no-stops "
            f"entries={len(batch_entries)}"
        )
        return

    chat_path = _chat_path_for_session(session_id, batch_entries)
    if not chat_path:
        debug_log(
            f"reupload_worker: aborted session_id={session_id} "
            f"reason=no-chat-sessions-path n_stops={n_stops}"
        )
        return

    # Poll until the last turn's tokens are flushed (early-exit), or until the
    # budget is exhausted.
    chat_requests = []
    waited = 0.0
    poll_count = 0
    while waited <= REUPLOAD_MAX_WAIT:
        parsed = parse_copilot_transcript(chat_path)
        chat_requests = parsed.get("requests", [])
        poll_count += 1
        if len(chat_requests) >= n_stops:
            last = chat_requests[n_stops - 1]
            if last.get("input_tokens") or last.get("output_tokens"):
                debug_log(
                    f"reupload_worker: early-exit session_id={session_id} "
                    f"polls={poll_count} waited={waited:.1f}s "
                    f"last_turn_input={last.get('input_tokens')} "
                    f"last_turn_output={last.get('output_tokens')}"
                )
                break
        time.sleep(REUPLOAD_POLL_INTERVAL)
        waited += REUPLOAD_POLL_INTERVAL
    else:
        debug_log(
            f"reupload_worker: budget-exhausted session_id={session_id} "
            f"polls={poll_count} waited={waited:.1f}s "
            f"chat_requests={len(chat_requests)} n_stops={n_stops}"
        )

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
    if overlay_changed:
        rewrite_batch(session_id, batch_entries)
    debug_log(
        f"reupload_worker: re-uploading session_id={session_id} "
        f"entries={len(batch_entries)} overlay_changed={overlay_changed}"
    )

    api_url = resolve_api_url()
    batch_payload = {
        "session_id": session_id,
        "source": "copilot",
        "plugin_version": PLUGIN_VERSION,
        "hooks": batch_entries,
    }
    upload_batch(api_url, api_key, batch_payload)


def main():
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
        debug_log(
            f"hook skipped: hook={hook_event_name} reason=no-session-id"
        )
        return

    runtime = detect_runtime(payload)
    debug_log(
        f"hook received: hook={hook_event_name} session_id={session_id} "
        f"runtime={runtime}"
    )

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
        # copilot-vscode  – payload.transcript_path points at the OLD format
        #   (GitHub.copilot-chat/transcripts/<uuid>.jsonl). It has messages
        #   and reasoning but NO tokens/model. Exact tokens+model live in
        #   chatSessions/<uuid>.jsonl, which VS Code flushes ~10-22 s after
        #   Stop. Two-phase parse + chatSessions overlay; the detached
        #   re-upload worker below handles the last-turn flush gap.
        #
        # copilot-cli     – payload.transcript_path points at
        #   ~/.copilot/session-state/<id>/events.jsonl, which is written
        #   synchronously and carries model, outputTokens, response content,
        #   and reasoning all at once. No async overlay needed; the CLI does
        #   not expose input tokens so those stay 0 and will be estimated.

        payload_path = payload.get("transcript_path", "")

        if runtime == "copilot-cli":
            parsed = parse_cli_transcript(payload_path) if payload_path else None
            requests = parsed.get("requests", []) if parsed else []
            current_req = (
                requests[-1] if len(requests) >= expected_turns else None
            )
            have_current_turn = current_req is not None and (
                current_req.get("response_content")
                or current_req.get("output_tokens")
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
            parsed = (
                parse_copilot_transcript(payload_path) if payload_path else None
            )
            requests = parsed.get("requests", []) if parsed else []
            have_current_turn = len(requests) >= expected_turns and requests[-1].get(
                "response_content"
            )
            current_req = (
                requests[-1] if len(requests) >= expected_turns else None
            )

            # --- Phase 2: chatSessions for tokens/model/IDs (best effort) ---
            chat_path = (
                derive_chat_sessions_path(payload_path)
                or find_copilot_transcript(session_id)
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
                        if ts
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
        summary = envelope.get("transcript_summary", {})
        calls = summary.get("api_calls", [{}])
        has_tokens = any(
            c.get("input_tokens") or c.get("output_tokens") for c in calls
        )
        if not has_tokens and runtime == "copilot-vscode":
            python_exe = sys.executable or "python3"
            spawned = spawn_detached(
                [python_exe, os.path.abspath(__file__), "__reupload", session_id]
            )
            debug_log(
                f"reupload_worker: spawned session_id={session_id} "
                f"success={spawned}"
            )
        else:
            debug_log(
                f"reupload_worker: not-spawned session_id={session_id} "
                f"runtime={runtime} has_tokens={has_tokens}"
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
