"""Parse Codex rollout JSONL files into Bloomfilter envelope shapes.

Codex hook payloads are sparse — they don't carry tool calls, thinking, token
usage, or intermediate assistant messages. The plugin reads the rollout
transcript at `payload.transcript_path` on Stop, extracts that data per turn,
and ships it alongside the hook envelope so the BE can build complete
AgentSession / AgentTurn / AgentEvent rows.

Public API:
    parse_session_meta(path) -> dict
    parse_turn(path, turn_id) -> dict
"""

import json
from datetime import datetime, timezone


def _read_lines(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def _parse_iso(ts):
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, AttributeError):
        return None


def _ms_between(a, b):
    if not a or not b:
        return None
    return int((b - a).total_seconds() * 1000)


def _decode_args(raw):
    """Codex ships function_call.arguments as a JSON string; decode if possible."""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def parse_session_meta(path):
    """Return session-level metadata fields from the rollout's session_meta line."""
    for entry in _read_lines(path):
        if entry.get("type") != "session_meta":
            continue
        p = entry.get("payload", {}) or {}
        return {
            "cli_version": p.get("cli_version") or "",
            "originator": p.get("originator") or "",
            "model_provider": p.get("model_provider") or "",
        }
    return {}


def parse_turn(path, turn_id):
    """Walk the rollout and extract per-turn data for the given turn_id.

    Returns a dict with:
        assistant_text   — concatenated assistant message text for the turn.
        thinking_blocks  — list of {timestamp, duration_ms} for each reasoning block.
        tool_calls       — list of {timestamp, tool_name, tool_input, tool_output,
                                    tool_call_id, exit_code, duration_ms} pairs.
        api_calls        — list of token-usage dicts ready for
                           transcript_summary.api_calls (BE-schema field names).
        model            — the model recorded for this turn's turn_context.
    """
    if not turn_id:
        return _empty_turn()

    # First pass: track turn_id boundaries, collect raw events.
    current_turn_id = None
    turn_model = ""
    assistant_chunks = []
    reasoning_starts = []  # list of (timestamp, prev_ts, api_call_seq_at_emit)
    function_calls = {}    # call_id -> {timestamp, tool_name, tool_input}
    function_outputs = {}  # call_id -> output string
    exec_meta = {}         # call_id -> {exit_code, duration_ms}
    api_calls = []
    last_event_ts = None   # used to estimate reasoning block duration
    pending_api_call_seq = 0  # sequence of the next token_count event in this turn
    time_to_first_token_ms = None  # captured from event_msg.task_complete

    def in_turn():
        return current_turn_id == turn_id

    for entry in _read_lines(path):
        etype = entry.get("type")
        ts = _parse_iso(entry.get("timestamp"))
        p = entry.get("payload", {}) or {}

        if etype == "turn_context":
            current_turn_id = p.get("turn_id") or current_turn_id
            if in_turn():
                turn_model = p.get("model") or turn_model
            continue

        if not in_turn():
            # response_item lines have no turn_id; bucket them into the
            # currently-active turn_context only.
            continue

        if etype == "event_msg":
            sub = p.get("type")
            # event_msg lines may carry their own turn_id (e.g. exec_command_end)
            ev_turn = p.get("turn_id")
            if ev_turn and ev_turn != turn_id:
                # Drop event_msg lines that explicitly belong to a different turn.
                continue

            # Note: event_msg.agent_message duplicates response_item.message
            # (UI streaming event vs canonical model output). Use only
            # response_item.message below to avoid doubling the text.
            if sub == "exec_command_end":
                cid = p.get("call_id")
                if cid:
                    duration = p.get("duration") or {}
                    secs = duration.get("secs", 0) or 0
                    nanos = duration.get("nanos", 0) or 0
                    duration_ms = int(secs * 1000 + nanos / 1_000_000)
                    exec_meta[cid] = {
                        "exit_code": p.get("exit_code"),
                        "duration_ms": duration_ms,
                    }
            elif sub == "token_count":
                info = p.get("info")
                if not info:
                    continue
                last = info.get("last_token_usage") or {}
                input_total = last.get("input_tokens", 0) or 0
                cached = last.get("cached_input_tokens", 0) or 0
                api_calls.append({
                    "input_tokens": max(input_total - cached, 0),
                    "output_tokens": last.get("output_tokens", 0) or 0,
                    "cache_read_tokens": cached,
                    "cache_creation_tokens": 0,
                    "model": turn_model,
                    "reasoning_output_tokens": last.get("reasoning_output_tokens", 0) or 0,
                })
                # token_count closes the current API-call window.
                pending_api_call_seq += 1
            elif sub == "task_complete":
                ttft = p.get("time_to_first_token_ms")
                if isinstance(ttft, (int, float)):
                    time_to_first_token_ms = int(ttft)
            last_event_ts = ts

        elif etype == "response_item":
            sub = p.get("type")
            if sub == "message":
                content = p.get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        text = block.get("text")
                        if text:
                            assistant_chunks.append(text)
            elif sub == "reasoning":
                reasoning_starts.append((ts, last_event_ts, pending_api_call_seq))
            elif sub in ("function_call", "custom_tool_call"):
                cid = p.get("call_id")
                if not cid:
                    continue
                function_calls[cid] = {
                    "timestamp": ts,
                    "tool_name": p.get("name") or sub,
                    "tool_input": _decode_args(
                        p.get("arguments") if "arguments" in p else p.get("input")
                    ),
                }
            elif sub in ("function_call_output", "custom_tool_call_output"):
                cid = p.get("call_id")
                if cid:
                    function_outputs[cid] = p.get("output") or p.get("result") or ""
            last_event_ts = ts

    # Build paired tool_calls
    tool_calls = []
    file_edits = []
    for cid, call in function_calls.items():
        meta = exec_meta.get(cid, {})
        tool_input = call["tool_input"]
        tool_output = function_outputs.get(cid, "")
        ts_iso = _to_iso(call["timestamp"])
        tool_calls.append({
            "timestamp": ts_iso,
            "tool_name": call["tool_name"],
            "tool_input": tool_input,
            "tool_output": tool_output,
            "tool_call_id": cid,
            "exit_code": meta.get("exit_code"),
            "duration_ms": meta.get("duration_ms"),
        })

        # apply_patch is Codex's primary file-edit mechanism. Its input is a
        # raw patch text covering one or more files; split it so each file
        # gets its own AgentFileEdit downstream.
        if call["tool_name"] == "apply_patch":
            patch_text = tool_input if isinstance(tool_input, str) else ""
            for op in parse_apply_patch(patch_text):
                file_edits.append({
                    "timestamp": ts_iso,
                    "tool_call_id": cid,
                    **op,
                })
    tool_calls.sort(key=lambda t: t["timestamp"] or "")
    file_edits.sort(key=lambda e: e["timestamp"] or "")

    # Build thinking blocks. Duration is best-effort: time between the
    # reasoning's preceding event and the reasoning timestamp itself.
    # reasoning_output_tokens is the call-level total for the API call this
    # block belongs to; multiple blocks in the same call share that total —
    # use api_call_seq to dedupe at aggregation time.
    thinking_blocks = []
    for ts, prev_ts, call_seq in reasoning_starts:
        call_idx = call_seq if 0 <= call_seq < len(api_calls) else None
        reasoning_tokens = (
            api_calls[call_idx].get("reasoning_output_tokens", 0)
            if call_idx is not None
            else 0
        )
        thinking_blocks.append({
            "timestamp": _to_iso(ts),
            "duration_ms": _ms_between(prev_ts, ts) if prev_ts and ts else None,
            "api_call_seq": call_idx,
            "reasoning_output_tokens": reasoning_tokens,
        })

    return {
        "assistant_text": "\n\n".join(c for c in assistant_chunks if c).strip(),
        "thinking_blocks": thinking_blocks,
        "tool_calls": tool_calls,
        "file_edits": file_edits,
        "api_calls": api_calls,
        "model": turn_model,
        "time_to_first_token_ms": time_to_first_token_ms,
    }


def _empty_turn():
    return {
        "assistant_text": "",
        "thinking_blocks": [],
        "tool_calls": [],
        "file_edits": [],
        "api_calls": [],
        "model": "",
        "time_to_first_token_ms": None,
    }


def parse_apply_patch(text):
    """Split a Codex apply_patch body into per-file operations.

    Codex's apply_patch grammar (mirrors OpenAI's reference patch tool):

        *** Begin Patch
        *** Update File: path
        @@
        -old
        +new
         context
        *** Add File: path
        +line1
        +line2
        *** Delete File: path
        *** End Patch

    Returns a list of dicts:
        {
          "file_path":       str,
          "file_action":     "CREATE" | "MODIFY" | "DELETE",
          "structured_patch": [
              {"old_start": int, "new_start": int,
               "old_lines": int, "new_lines": int,
               "lines": ["+...", "-...", " ..."]}
          ],
        }

    `structured_patch` matches the BE-native shape consumed by
    FileEditExtractor.count_added_lines / count_removed_lines.
    """
    if not isinstance(text, str) or "*** Begin Patch" not in text:
        return []

    # Strip the outer markers if present; tolerate inputs without them.
    body = text
    start_idx = body.find("*** Begin Patch")
    if start_idx >= 0:
        body = body[start_idx + len("*** Begin Patch"):]
    end_idx = body.rfind("*** End Patch")
    if end_idx >= 0:
        body = body[:end_idx]

    lines = body.split("\n")

    ops = []
    current = None
    hunks = None
    hunk_lines = None

    def _flush():
        nonlocal current, hunks, hunk_lines
        if current is None:
            return
        if hunk_lines is not None:
            hunks.append(_finalize_hunk(hunk_lines))
            hunk_lines = None
        current["structured_patch"] = hunks or []
        ops.append(current)
        current = None
        hunks = None

    for line in lines:
        # File-op headers
        if line.startswith("*** Update File:"):
            _flush()
            current = {
                "file_path": line[len("*** Update File:"):].strip(),
                "file_action": "MODIFY",
            }
            hunks = []
            hunk_lines = None
            continue
        if line.startswith("*** Add File:"):
            _flush()
            current = {
                "file_path": line[len("*** Add File:"):].strip(),
                "file_action": "CREATE",
            }
            hunks = []
            hunk_lines = []  # add-files contribute a single all-additions hunk
            continue
        if line.startswith("*** Delete File:"):
            _flush()
            current = {
                "file_path": line[len("*** Delete File:"):].strip(),
                "file_action": "DELETE",
            }
            hunks = []
            hunk_lines = None
            continue

        if current is None:
            # Pre-amble or noise between markers.
            continue

        if current["file_action"] == "MODIFY":
            if line.startswith("@@"):
                if hunk_lines is not None:
                    hunks.append(_finalize_hunk(hunk_lines))
                hunk_lines = []
                continue
            if hunk_lines is None:
                # Update body without an explicit @@ — start an implicit hunk.
                hunk_lines = []
            if line.startswith(("+", "-", " ")):
                hunk_lines.append(line)
            elif line == "":
                hunk_lines.append(" ")  # blank context line
        elif current["file_action"] == "CREATE":
            if line.startswith("+"):
                hunk_lines.append(line)
            elif line == "":
                hunk_lines.append("+")
        # DELETE has no body lines

    _flush()
    return ops


def _finalize_hunk(lines):
    """Wrap a list of patch lines in the structured_patch hunk shape."""
    old_lines = sum(1 for l in lines if l.startswith(("-", " ")))
    new_lines = sum(1 for l in lines if l.startswith(("+", " ")))
    return {
        "old_start": 1,
        "new_start": 1,
        "old_lines": old_lines,
        "new_lines": new_lines,
        "lines": lines,
    }


def _to_iso(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
