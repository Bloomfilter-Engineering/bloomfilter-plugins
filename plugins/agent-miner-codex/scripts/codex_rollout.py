from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator


def _read_lines(path: str) -> Iterator[dict[str, Any]]:
    """Yield each non-empty JSON object from a rollout JSONL file."""
    with open(path) as rollout_file:
        for raw_line in rollout_file:
            stripped_line = raw_line.strip()
            if not stripped_line:
                continue
            try:
                yield json.loads(stripped_line)
            except (json.JSONDecodeError, ValueError):
                continue


def _parse_iso(timestamp_string: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string (with optional trailing Z) to datetime."""
    if not timestamp_string:
        return None
    try:
        normalized = timestamp_string
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except (ValueError, AttributeError):
        return None


def _ms_between(start: datetime | None, end: datetime | None) -> int | None:
    """Return milliseconds between two datetimes, or None if either is missing."""
    if not start or not end:
        return None
    return int((end - start).total_seconds() * 1000)


def _decode_arguments(raw_arguments: Any) -> Any:
    """Codex ships function_call.arguments as a JSON string; decode if possible."""
    if isinstance(raw_arguments, (dict, list)):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return raw_arguments
    try:
        return json.loads(raw_arguments)
    except (json.JSONDecodeError, ValueError):
        return raw_arguments


def parse_session_meta(path: str) -> dict[str, str]:
    """Return session-level metadata fields from the rollout's session_meta line."""
    for entry in _read_lines(path):
        if entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload", {}) or {}
        return {
            "cli_version": payload.get("cli_version") or "",
            "originator": payload.get("originator") or "",
            "model_provider": payload.get("model_provider") or "",
        }
    return {}


def parse_turn(path: str, turn_id: str) -> dict[str, Any]:
    """Walk the rollout and extract per-turn data for the given turn_id.

    Returns a dict with:
        assistant_text         — concatenated assistant message text for the turn.
        thinking_blocks        — list of {timestamp, duration_ms, api_call_seq,
                                          reasoning_output_tokens} per reasoning block.
        tool_calls             — list of {timestamp, tool_name, tool_input, tool_output,
                                          tool_call_id, exit_code, duration_ms} pairs.
        file_edits             — list of apply_patch-derived per-file ops.
        api_calls              — list of token-usage dicts ready for
                                 transcript_summary.api_calls (BE-schema field names).
        model                  — the model recorded for this turn's turn_context.
        time_to_first_token_ms — captured from event_msg.task_complete.
    """
    if not turn_id:
        return _empty_turn()

    # First pass: track turn_id boundaries, collect raw events.
    current_turn_id: str | None = None
    turn_model: str = ""
    assistant_chunks: list[str] = []
    # list of (reasoning_timestamp, previous_event_timestamp, api_call_seq_at_emit)
    reasoning_starts: list[tuple[datetime | None, datetime | None, int]] = []
    function_calls: dict[str, dict[str, Any]] = {}
    function_outputs: dict[str, str] = {}
    execution_metadata: dict[str, dict[str, Any]] = {}
    api_calls: list[dict[str, Any]] = []
    last_event_timestamp: datetime | None = None
    pending_api_call_seq: int = 0
    time_to_first_token_ms: int | None = None

    def in_turn() -> bool:
        return current_turn_id == turn_id

    for entry in _read_lines(path):
        entry_type = entry.get("type")
        entry_timestamp = _parse_iso(entry.get("timestamp"))
        payload = entry.get("payload", {}) or {}

        if entry_type == "turn_context":
            current_turn_id = payload.get("turn_id") or current_turn_id
            if in_turn():
                turn_model = payload.get("model") or turn_model
            continue

        if not in_turn():
            # response_item lines have no turn_id; bucket them into the
            # currently-active turn_context only.
            continue

        if entry_type == "event_msg":
            event_subtype = payload.get("type")
            # event_msg lines may carry their own turn_id (e.g. exec_command_end)
            event_turn_id = payload.get("turn_id")
            if event_turn_id and event_turn_id != turn_id:
                # Drop event_msg lines that explicitly belong to a different turn.
                continue

            # Note: event_msg.agent_message duplicates response_item.message
            # (UI streaming event vs canonical model output). Use only
            # response_item.message below to avoid doubling the text.
            match event_subtype:
                case "exec_command_end":
                    call_id = payload.get("call_id")
                    if call_id:
                        duration = payload.get("duration") or {}
                        seconds = duration.get("secs", 0) or 0
                        nanoseconds = duration.get("nanos", 0) or 0
                        duration_ms = int(seconds * 1000 + nanoseconds / 1_000_000)
                        execution_metadata[call_id] = {
                            "exit_code": payload.get("exit_code"),
                            "duration_ms": duration_ms,
                        }
                case "token_count":
                    token_info = payload.get("info")
                    if not token_info:
                        continue
                    last_token_usage = token_info.get("last_token_usage") or {}
                    total_input_tokens = last_token_usage.get("input_tokens", 0) or 0
                    cached_input_tokens = (
                        last_token_usage.get("cached_input_tokens", 0) or 0
                    )
                    api_calls.append(
                        {
                            "input_tokens": max(
                                total_input_tokens - cached_input_tokens, 0
                            ),
                            "output_tokens": (
                                last_token_usage.get("output_tokens", 0) or 0
                            ),
                            "cache_read_tokens": cached_input_tokens,
                            "cache_creation_tokens": 0,
                            "model": turn_model,
                            "reasoning_output_tokens": (
                                last_token_usage.get("reasoning_output_tokens", 0) or 0
                            ),
                        }
                    )
                    # token_count closes the current API-call window.
                    pending_api_call_seq += 1
                case "task_complete":
                    raw_ttft = payload.get("time_to_first_token_ms")
                    if isinstance(raw_ttft, (int, float)):
                        time_to_first_token_ms = int(raw_ttft)
            last_event_timestamp = entry_timestamp

        elif entry_type == "response_item":
            response_subtype = payload.get("type")
            if response_subtype == "message":
                content_blocks = payload.get("content") or []
                for content_block in content_blocks:
                    if (
                        isinstance(content_block, dict)
                        and content_block.get("type") == "output_text"
                    ):
                        text_value = content_block.get("text")
                        if text_value:
                            assistant_chunks.append(text_value)
            elif response_subtype == "reasoning":
                reasoning_starts.append(
                    (entry_timestamp, last_event_timestamp, pending_api_call_seq)
                )
            elif response_subtype in ("function_call", "custom_tool_call"):
                call_id = payload.get("call_id")
                if not call_id:
                    continue
                raw_input = (
                    payload.get("arguments")
                    if "arguments" in payload
                    else payload.get("input")
                )
                function_calls[call_id] = {
                    "timestamp": entry_timestamp,
                    "tool_name": payload.get("name") or response_subtype,
                    "tool_input": _decode_arguments(raw_input),
                }
            elif response_subtype in (
                "function_call_output",
                "custom_tool_call_output",
            ):
                call_id = payload.get("call_id")
                if call_id:
                    function_outputs[call_id] = (
                        payload.get("output") or payload.get("result") or ""
                    )
            last_event_timestamp = entry_timestamp

    # Build paired tool_calls
    tool_calls: list[dict[str, Any]] = []
    file_edits: list[dict[str, Any]] = []
    for call_id, call_data in function_calls.items():
        execution_data = execution_metadata.get(call_id, {})
        tool_input = call_data["tool_input"]
        tool_output = function_outputs.get(call_id, "")
        timestamp_iso = _to_iso(call_data["timestamp"])
        tool_calls.append(
            {
                "timestamp": timestamp_iso,
                "tool_name": call_data["tool_name"],
                "tool_input": tool_input,
                "tool_output": tool_output,
                "tool_call_id": call_id,
                "exit_code": execution_data.get("exit_code"),
                "duration_ms": execution_data.get("duration_ms"),
            }
        )

        # apply_patch is Codex's primary file-edit mechanism. Its input is a
        # raw patch text covering one or more files; split it so each file
        # gets its own AgentFileEdit downstream.
        if call_data["tool_name"] == "apply_patch":
            patch_text = tool_input if isinstance(tool_input, str) else ""
            for file_operation in parse_apply_patch(patch_text):
                file_edits.append(
                    {
                        "timestamp": timestamp_iso,
                        "tool_call_id": call_id,
                        **file_operation,
                    }
                )
    tool_calls.sort(key=lambda tool_call: tool_call["timestamp"] or "")
    file_edits.sort(key=lambda file_edit: file_edit["timestamp"] or "")

    # Build thinking blocks. Duration is best-effort: time between the
    # reasoning's preceding event and the reasoning timestamp itself.
    # reasoning_output_tokens is the call-level total for the API call this
    # block belongs to; multiple blocks in the same call share that total —
    # use api_call_seq to dedupe at aggregation time.
    thinking_blocks: list[dict[str, Any]] = []
    for (
        reasoning_timestamp,
        previous_event_timestamp,
        api_call_sequence,
    ) in reasoning_starts:
        api_call_index: int | None = (
            api_call_sequence if 0 <= api_call_sequence < len(api_calls) else None
        )
        reasoning_output_tokens = (
            api_calls[api_call_index].get("reasoning_output_tokens", 0)
            if api_call_index is not None
            else 0
        )
        thinking_blocks.append(
            {
                "timestamp": _to_iso(reasoning_timestamp),
                "duration_ms": (
                    _ms_between(previous_event_timestamp, reasoning_timestamp)
                    if previous_event_timestamp and reasoning_timestamp
                    else None
                ),
                "api_call_seq": api_call_index,
                "reasoning_output_tokens": reasoning_output_tokens,
            }
        )

    return {
        "assistant_text": "\n\n".join(
            chunk for chunk in assistant_chunks if chunk
        ).strip(),
        "thinking_blocks": thinking_blocks,
        "tool_calls": tool_calls,
        "file_edits": file_edits,
        "api_calls": api_calls,
        "model": turn_model,
        "time_to_first_token_ms": time_to_first_token_ms,
    }


def _empty_turn() -> dict[str, Any]:
    return {
        "assistant_text": "",
        "thinking_blocks": [],
        "tool_calls": [],
        "file_edits": [],
        "api_calls": [],
        "model": "",
        "time_to_first_token_ms": None,
    }


def parse_apply_patch(patch_text: str) -> list[dict[str, Any]]:
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
    if not isinstance(patch_text, str) or "*** Begin Patch" not in patch_text:
        return []

    # Strip the outer markers if present; tolerate inputs without them.
    body = patch_text
    begin_index = body.find("*** Begin Patch")
    if begin_index >= 0:
        body = body[begin_index + len("*** Begin Patch") :]
    end_index = body.rfind("*** End Patch")
    if end_index >= 0:
        body = body[:end_index]

    lines = body.split("\n")

    operations: list[dict[str, Any]] = []
    current_operation: dict[str, Any] | None = None
    current_hunks: list[dict[str, Any]] | None = None
    current_hunk_lines: list[str] | None = None

    def flush_current_operation() -> None:
        nonlocal current_operation, current_hunks, current_hunk_lines
        if current_operation is None:
            return
        if current_hunk_lines is not None:
            current_hunks.append(_finalize_hunk(current_hunk_lines))
            current_hunk_lines = None
        current_operation["structured_patch"] = current_hunks or []
        operations.append(current_operation)
        current_operation = None
        current_hunks = None

    for line in lines:
        # File-op headers
        if line.startswith("*** Update File:"):
            flush_current_operation()
            current_operation = {
                "file_path": line[len("*** Update File:") :].strip(),
                "file_action": "MODIFY",
            }
            current_hunks = []
            current_hunk_lines = None
            continue
        if line.startswith("*** Add File:"):
            flush_current_operation()
            current_operation = {
                "file_path": line[len("*** Add File:") :].strip(),
                "file_action": "CREATE",
            }
            current_hunks = []
            # add-files contribute a single all-additions hunk
            current_hunk_lines = []
            continue
        if line.startswith("*** Delete File:"):
            flush_current_operation()
            current_operation = {
                "file_path": line[len("*** Delete File:") :].strip(),
                "file_action": "DELETE",
            }
            current_hunks = []
            current_hunk_lines = None
            continue

        if current_operation is None:
            # Pre-amble or noise between markers.
            continue

        if current_operation["file_action"] == "MODIFY":
            if line.startswith("@@"):
                if current_hunk_lines is not None:
                    current_hunks.append(_finalize_hunk(current_hunk_lines))
                current_hunk_lines = []
                continue
            if current_hunk_lines is None:
                # Update body without an explicit @@ — start an implicit hunk.
                current_hunk_lines = []
            if line.startswith(("+", "-", " ")):
                current_hunk_lines.append(line)
            elif line == "":
                current_hunk_lines.append(" ")  # blank context line
        elif current_operation["file_action"] == "CREATE":
            if line.startswith("+"):
                current_hunk_lines.append(line)
            elif line == "":
                current_hunk_lines.append("+")
        # DELETE has no body lines

    flush_current_operation()
    return operations


def _finalize_hunk(hunk_lines: list[str]) -> dict[str, Any]:
    """Wrap a list of patch lines in the structured_patch hunk shape."""
    old_lines_count = sum(1 for line in hunk_lines if line.startswith(("-", " ")))
    new_lines_count = sum(1 for line in hunk_lines if line.startswith(("+", " ")))
    return {
        "old_start": 1,
        "new_start": 1,
        "old_lines": old_lines_count,
        "new_lines": new_lines_count,
        "lines": hunk_lines,
    }


def _to_iso(datetime_value: datetime | None) -> str:
    """Serialize a datetime to a UTC ISO 8601 string, or '' if missing."""
    if not datetime_value:
        return ""
    if datetime_value.tzinfo is None:
        datetime_value = datetime_value.replace(tzinfo=timezone.utc)
    return datetime_value.astimezone(timezone.utc).isoformat()
