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
    derive_chat_sessions_path,
    find_copilot_transcript,
    get_git_branch,
    parse_copilot_transcript,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    rewrite_batch,
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


def main():
    hook_event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    if not hook_event_name:
        return

    payload = read_payload()
    session_id = payload.get("session_id", "")
    if not session_id:
        return

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
            return

    # Clear stale batch on SessionStart (new session = fresh batch)
    if hook_event_name == "SessionStart":
        clear_batch(session_id)

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

        # Two transcript locations with different timing and data:
        #
        #   transcripts/   (payload.transcript_path)
        #     Written synchronously — available immediately on Stop.
        #     Has response content and reasoning, NO tokens or model.
        #
        #   chatSessions/  (derived or discovered)
        #     Written ~15 s after Stop (VS Code flushes late).
        #     Has everything: content, tokens, model, IDs.
        #     Previous turns' data IS ready — only current turn delayed.
        #
        # Strategy: parse old transcript for messages (critical, fast),
        # then overlay token/model/ID data from chatSessions.  Backfill
        # previous turns' tokens from chatSessions on each Stop.

        # --- Phase 1: Old transcript for messages (retry until ready) ---
        payload_path = payload.get("transcript_path", "")
        parsed = None
        max_wait = 3.0
        waited = 0.0
        while waited < max_wait:
            if payload_path:
                parsed = parse_copilot_transcript(payload_path)
                requests = parsed.get("requests", [])
                if len(requests) >= expected_turns and requests[-1].get(
                    "response_content"
                ):
                    break
            time.sleep(0.5)
            waited += 0.5

        requests = parsed.get("requests", []) if parsed else []
        have_current_turn = len(requests) >= expected_turns and requests[-1].get(
            "response_content"
        )
        current_req = requests[-1] if len(requests) >= expected_turns else None

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
            return

        api_url = resolve_api_url()
        entries = read_batch(session_id)
        if not entries:
            return

        batch_payload = {
            "session_id": session_id,
            "source": "copilot",
            "plugin_version": PLUGIN_VERSION,
            "hooks": entries,
        }

        upload_batch(api_url, api_key, batch_payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[bloomfilter] collect_hook failed: {exc}", file=sys.stderr)
    sys.exit(0)
