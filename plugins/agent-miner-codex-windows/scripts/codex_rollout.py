from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator


def _read_lines(path: str) -> Iterator[dict[str, Any]]:
    """Yield each non-empty JSON object from a rollout JSONL file.

    Codex writes rollouts as UTF-8 from its Rust core; we open explicitly so
    Windows users with non-UTF-8 locales don't fall back to cp1252 etc.
    `errors="replace"` keeps a single bad byte from killing the whole parse —
    affected lines just fail JSON decoding (caught below) and are skipped.
    Non-object JSON values (arrays/scalars) are filtered out so callers can
    rely on dict-shaped entries.

    Args:
        path: Rollout JSONL file to read.

    Yields:
        Each line that decodes to a JSON object, in file order. Blank lines,
        undecodable lines and non-object JSON values are skipped silently.
    """
    with open(path, encoding="utf-8", errors="replace") as rollout_file:
        for raw_line in rollout_file:
            stripped_line = raw_line.strip()
            if not stripped_line:
                continue
            try:
                parsed_value = json.loads(stripped_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed_value, dict):
                yield parsed_value


def _parse_iso(timestamp_string: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string (with optional trailing Z) to datetime.

    Args:
        timestamp_string: Timestamp to parse. A trailing ``Z`` is rewritten to
            ``+00:00`` first, which :func:`datetime.fromisoformat` accepts.

    Returns:
        The parsed datetime, or None when the input is empty or unparseable.
    """
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
    """Return milliseconds between two datetimes, or None if either is missing.

    Args:
        start: Start of the span.
        end: End of the span.

    Returns:
        Whole milliseconds from *start* to *end*, or None when either bound is
        missing. A negative span is returned as-is rather than clamped.
    """
    if not start or not end:
        return None
    return int((end - start).total_seconds() * 1000)


def _decode_arguments(raw_arguments: Any) -> Any:
    """Codex ships function_call.arguments as a JSON string; decode if possible.

    Args:
        raw_arguments: The recorded ``function_call.arguments`` value, normally
            a JSON string but tolerated as any type.

    Returns:
        The decoded object when *raw_arguments* is a string holding valid JSON,
        otherwise the value unchanged — an undecodable string is passed through
        so the tool call is still reported. Only JSONDecodeError and ValueError
        are caught, so pathologically nested input can still raise
        RecursionError.
    """
    if isinstance(raw_arguments, (dict, list)):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return raw_arguments
    try:
        return json.loads(raw_arguments)
    except (json.JSONDecodeError, ValueError):
        return raw_arguments


def parse_session_meta(path: str) -> dict[str, str]:
    """Return session-level metadata fields from the rollout's session_meta line.

    Args:
        path: Rollout JSONL file to read.

    Returns:
        ``cli_version``, ``originator`` and ``model_provider`` from the first
        ``session_meta`` entry, each defaulting to ''. An empty dict when the
        rollout has no such entry.
    """
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
                                 transcript_summary.api_calls, using the API's field names.
        model                  — the model recorded for this turn's turn_context.
        time_to_first_token_ms — captured from event_msg.task_complete.

    Args:
        path: Rollout JSONL file to read.
        turn_id: The ``turn_context`` turn to extract. An empty value yields
            the empty-turn shape rather than scanning the file.

    Returns:
        The per-turn dict described above, with every key always present.
    """
    if not turn_id:
        return _empty_turn()
    return _build_turn(list(_read_lines(path)), turn_id)


def parse_transcript(path: str) -> dict[str, Any]:
    """Build a normalized subagent transcript from a whole Codex rollout.

    Returns ``{"turns": [...]}`` — one turn per ``turn_context`` turn_id, in
    first-seen order, each shaped for the Bloomfilter backend's child-session
    builder (``user_prompt``, ``agent_response``, per-turn token totals,
    ``tool_calls``). Per-turn token totals are summed from the turn's
    ``token_count`` events, matching how the main-session path aggregates
    ``api_calls``. The rollout is read once and reused across all turns.

    Args:
        path: Rollout JSONL file to read.

    Returns:
        ``{"turns": [...]}`` — one turn per ``turn_context`` turn_id, in
        first-seen order. The list is empty when the rollout records no turns.
    """
    entries = list(_read_lines(path))
    turns = [
        _to_subagent_turn(_build_turn(entries, turn_id))
        for turn_id in _iter_turn_ids(entries)
    ]
    return {"turns": turns}


def _iter_turn_ids(entries: list[dict[str, Any]]) -> list[str]:
    """Return turn_context turn_ids in first-seen order.

    Args:
        entries: Rollout entries already read into memory.

    Returns:
        Each distinct ``turn_context`` turn_id, in the order it first appears.
    """
    seen: list[str] = []
    for entry in entries:
        if entry.get("type") != "turn_context":
            continue
        turn_id = (entry.get("payload") or {}).get("turn_id")
        if turn_id and turn_id not in seen:
            seen.append(turn_id)
    return seen


def _to_subagent_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """Map a ``_build_turn`` result to the backend's child-turn shape.

    Token totals sum the turn's ``api_calls`` (each already
    input-minus-cached) so accounting matches the main-session path. Codex
    exposes no cache-creation figure, so ``cache_creation_tokens`` is 0.

    Args:
        turn: A :func:`_build_turn` result for one turn.

    Returns:
        The same turn in the backend's child-turn shape: prompt, response,
        per-turn token totals and tool calls.
    """
    api_calls = turn.get("api_calls") or []
    input_tokens = sum(
        int(api_call.get("input_tokens", 0) or 0) for api_call in api_calls
    )
    output_tokens = sum(
        int(api_call.get("output_tokens", 0) or 0) for api_call in api_calls
    )
    cache_read_tokens = sum(
        int(api_call.get("cache_read_tokens", 0) or 0) for api_call in api_calls
    )
    tool_calls = [
        {
            "started_at": tool_call.get("timestamp") or "",
            "tool_name": tool_call.get("tool_name", ""),
            "tool_call_id": tool_call.get("tool_call_id", ""),
            "tool_input": tool_call.get("tool_input"),
            "tool_output": tool_call.get("tool_output"),
        }
        for tool_call in (turn.get("tool_calls") or [])
    ]
    return {
        "started_at": turn.get("started_at") or "",
        "ended_at": turn.get("ended_at") or "",
        "model": turn.get("model") or "",
        "user_prompt": turn.get("user_prompt") or None,
        "agent_response": turn.get("assistant_text") or None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": 0,
        "tool_calls": tool_calls,
    }


def _build_turn(entries: list[dict[str, Any]], turn_id: str) -> dict[str, Any]:
    """Extract per-turn data for ``turn_id`` from pre-read rollout entries.

    Args:
        entries: Rollout entries already read into memory, in file order. The
            walk tracks turn boundaries itself, so the whole rollout is passed
            rather than a pre-filtered slice.
        turn_id: The ``turn_context`` turn to extract.

    Returns:
        The same shape as :func:`parse_turn`, with every key always present so
        callers never have to test for absence.
    """
    # First pass: track turn_id boundaries, collect raw events.
    current_turn_id: str | None = None
    turn_model: str = ""
    assistant_chunks: list[str] = []
    assistant_messages: list[dict[str, Any]] = []
    turn_started_at: datetime | None = None
    turn_ended_at: datetime | None = None
    user_prompt_text: str = ""
    # Each item is (reasoning_timestamp, previous_event_timestamp,
    # api_call_sequence_at_emit).
    reasoning_starts: list[tuple[datetime | None, datetime | None, int]] = []
    function_calls: dict[str, dict[str, Any]] = {}
    function_outputs: dict[str, str] = {}
    execution_metadata: dict[str, dict[str, Any]] = {}
    api_calls: list[dict[str, Any]] = []
    last_event_timestamp: datetime | None = None
    pending_api_call_sequence: int = 0
    time_to_first_token_ms: int | None = None

    def in_turn() -> bool:
        """Report whether the walk is currently inside the requested turn.

        Returns:
            True while the most recent ``turn_context`` entry named *turn_id*.
            Entries that carry no turn of their own are attributed to whichever
            turn is open, so this is the gate for collecting them.
        """
        return current_turn_id == turn_id

    for entry in entries:
        entry_type = entry.get("type")
        entry_timestamp = _parse_iso(entry.get("timestamp"))
        payload = entry.get("payload", {}) or {}

        if entry_type == "turn_context":
            current_turn_id = payload.get("turn_id") or current_turn_id
            if in_turn():
                turn_model = payload.get("model") or turn_model
                if entry_timestamp is not None and turn_started_at is None:
                    turn_started_at = entry_timestamp
                    turn_ended_at = entry_timestamp
            continue

        if not in_turn():
            # response_item lines have no turn_id; bucket them into the
            # currently-active turn_context only.
            continue

        # event_msg lines may carry their own turn_id (e.g. exec_command_end).
        # Drop those explicitly belonging to a different turn BEFORE they can
        # influence this turn's wall-clock span below.
        if entry_type == "event_msg":
            event_turn_id = payload.get("turn_id")
            if event_turn_id and event_turn_id != turn_id:
                continue

        # Track the turn's wall-clock span from its in-turn entries.
        if entry_timestamp is not None:
            if turn_started_at is None:
                turn_started_at = entry_timestamp
            turn_ended_at = entry_timestamp

        match entry_type:
            case "event_msg":
                event_subtype = payload.get("type")
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
                        total_input_tokens = (
                            last_token_usage.get("input_tokens", 0) or 0
                        )
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
                                    last_token_usage.get("reasoning_output_tokens", 0)
                                    or 0
                                ),
                            }
                        )
                        # token_count closes the current API-call window.
                        pending_api_call_sequence += 1
                    case "task_complete":
                        raw_time_to_first_token_ms = payload.get(
                            "time_to_first_token_ms"
                        )
                        if isinstance(raw_time_to_first_token_ms, (int, float)):
                            time_to_first_token_ms = int(raw_time_to_first_token_ms)
                    case "user_message":
                        # The turn's user prompt (subagent transcript needs it).
                        # First non-empty wins so an inherited/forked turn keeps
                        # its opening prompt rather than a later replay.
                        message_text = payload.get("message")
                        if (
                            isinstance(message_text, str)
                            and message_text
                            and not user_prompt_text
                        ):
                            user_prompt_text = message_text
                last_event_timestamp = entry_timestamp

            case "response_item":
                response_subtype = payload.get("type")
                match response_subtype:
                    case "message":
                        content_blocks = payload.get("content") or []
                        message_texts = [
                            content_block.get("text")
                            for content_block in content_blocks
                            if isinstance(content_block, dict)
                            and content_block.get("type") == "output_text"
                            and content_block.get("text")
                        ]
                        if message_texts:
                            assistant_chunks.extend(message_texts)
                            # Record the whole message as one narration segment with
                            # its timestamp so the caller can interleave intermediate
                            # narration around tool/subagent calls instead of merging
                            # it all into the final response.
                            assistant_messages.append(
                                {
                                    "timestamp": _to_iso(entry_timestamp),
                                    "text": "\n".join(message_texts),
                                }
                            )
                    case "reasoning":
                        reasoning_starts.append(
                            (
                                entry_timestamp,
                                last_event_timestamp,
                                pending_api_call_sequence,
                            )
                        )
                    case "function_call" | "custom_tool_call":
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
                    case "function_call_output" | "custom_tool_call_output":
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

        # apply_patch is Codex's primary file-edit mechanism. Its body covers
        # one or more files; split it so each file gets its own AgentFileEdit
        # downstream. The tool is named directly on older builds and wrapped in
        # a JavaScript shim under `exec` on current ones, so the body is
        # extracted rather than read straight off the input.
        patch_text = extract_patch_text(call_data["tool_name"], tool_input)
        if patch_text:
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
    # use the api_call_seq field to de-duplicate at aggregation time.
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
        "user_prompt": user_prompt_text or None,
        "assistant_messages": assistant_messages,
        "started_at": _to_iso(turn_started_at),
        "ended_at": _to_iso(turn_ended_at),
    }


def _empty_turn() -> dict[str, Any]:
    """Return the turn shape with every field at its empty value.

    Returns:
        A turn dict carrying no data. Used when there is no turn to parse, so
        callers can read every key without testing for absence first.
    """
    return {
        "assistant_text": "",
        "thinking_blocks": [],
        "tool_calls": [],
        "file_edits": [],
        "api_calls": [],
        "model": "",
        "time_to_first_token_ms": None,
        "user_prompt": None,
        "assistant_messages": [],
        "started_at": "",
        "ended_at": "",
    }


JS_STRING_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}


def _unescape_js_string(literal: str) -> str:
    """Resolve JavaScript escape sequences in a string-literal body.

    Used only for literals ``json.loads`` cannot take — single-quoted and
    template literals, and any literal carrying a JavaScript-only escape such
    as ``\\'``.

    Args:
        literal: The literal's contents, without its surrounding quotes.

    Returns:
        The literal with its escape sequences resolved. An unrecognised escape
        yields the escaped character itself, matching JavaScript.
    """
    decoded: list[str] = []
    index = 0
    while index < len(literal):
        character = literal[index]
        if character != "\\" or index + 1 >= len(literal):
            decoded.append(character)
            index += 1
            continue
        following = literal[index + 1]
        if following == "u" and index + 6 <= len(literal):
            try:
                decoded.append(chr(int(literal[index + 2 : index + 6], 16)))
            except ValueError:
                decoded.append(following)
            else:
                index += 6
                continue
        decoded.append(JS_STRING_ESCAPES.get(following, following))
        index += 2
    return "".join(decoded)


def _decode_js_string_literal(source: str, needle: str) -> str:
    """Decode the JavaScript string literal in *source* that contains *needle*.

    Args:
        source: JavaScript source recorded as a tool input.
        needle: Text known to sit inside the literal of interest.

    Returns:
        The literal with its escapes resolved, or an empty string when the
        surrounding quotes cannot be located.
    """
    needle_index = source.find(needle)
    if needle_index < 0:
        return ""

    open_index = -1
    for position in range(needle_index - 1, -1, -1):
        if source[position] in "\"'`":
            open_index = position
            break
    if open_index < 0:
        return ""

    quote = source[open_index]
    position = open_index + 1
    while position < len(source):
        if source[position] == "\\":
            position += 2
            continue
        if source[position] == quote:
            break
        position += 1
    else:
        return ""

    literal = source[open_index + 1 : position]
    if quote == '"':
        try:
            return json.loads(f'"{literal}"')
        except ValueError:
            pass
    return _unescape_js_string(literal)


def extract_patch_text(tool_name: str, tool_input: Any) -> str:
    """Return the apply_patch body a tool call carries, or an empty string.

    Codex has shipped two shapes. Older builds exposed apply_patch as a named
    tool whose input was the raw patch. Current builds (observed on codex-cli
    0.150.1) run it through a JavaScript shim under ``exec``, so the patch
    arrives as an escaped string literal whose newlines are two-character
    ``\\n`` sequences — unreadable to the grammar parser until decoded.

    Args:
        tool_name: Tool name recorded for the call.
        tool_input: Tool input recorded for the call.

    Returns:
        The decoded patch text, or an empty string when the call carries no
        applied patch.
    """
    if not isinstance(tool_input, str) or not tool_input:
        return ""
    if tool_name == "apply_patch":
        return tool_input
    # Require a real invocation: a script that only builds a patch string and
    # never applies it changed nothing, so it must not be recorded as an edit.
    if "*** Begin Patch" not in tool_input or "apply_patch(" not in tool_input:
        return ""
    return _decode_js_string_literal(tool_input, "*** Begin Patch")


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

    `structured_patch` matches the shape consumed by
    FileEditExtractor.count_added_lines / count_removed_lines.

    Args:
        patch_text: A full ``apply_patch`` payload. The opening
            ``*** Begin Patch`` marker is REQUIRED — an unwrapped body is
            rejected rather than parsed, so callers must not strip it. The
            closing ``*** End Patch`` marker is optional.

    Returns:
        One dict per file touched, in patch order. An empty list when
        *patch_text* is not a string or carries no ``*** Begin Patch`` marker.
    """
    if not isinstance(patch_text, str) or "*** Begin Patch" not in patch_text:
        return []

    # Strip the outer markers. Only the closing one can legitimately be absent
    # here — the guard above already rejected anything lacking an opening
    # ``*** Begin Patch``.
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
        """Close the operation being built and append it to *operations*.

        Finalizes any hunk still open, then resets the operation, hunk list and
        hunk-line accumulators. A no-op when no operation is open, so it is
        safe to call before every file header and once at the end.
        """
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

        match current_operation["file_action"]:
            case "MODIFY":
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
            case "CREATE":
                if line.startswith("+"):
                    current_hunk_lines.append(line)
                elif line == "":
                    current_hunk_lines.append("+")
            # DELETE has no body lines

    flush_current_operation()
    return operations


def _finalize_hunk(hunk_lines: list[str]) -> dict[str, Any]:
    """Wrap a list of patch lines in the structured_patch hunk shape.

    Args:
        hunk_lines: Patch lines for one hunk, each prefixed '+', '-' or ' '.

    Returns:
        The hunk in ``structured_patch`` shape. ``old_start`` and ``new_start``
        are always 1: Codex's grammar carries no line numbers, and the consumer
        counts added and removed lines rather than locating them.
    """
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
    """Serialize a datetime to a UTC ISO 8601 string, or '' if missing.

    Args:
        datetime_value: The datetime to serialize. A naive value is read as
            already being UTC rather than local time.

    Returns:
        The UTC ISO 8601 representation, or '' when *datetime_value* is None.
    """
    if not datetime_value:
        return ""
    if datetime_value.tzinfo is None:
        datetime_value = datetime_value.replace(tzinfo=timezone.utc)
    return datetime_value.astimezone(timezone.utc).isoformat()
