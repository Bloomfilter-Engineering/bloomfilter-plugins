from __future__ import annotations

import json
import re
from typing import Any

# The opening user message wraps the real prompt in these tags, e.g.
# "<timestamp>...</timestamp>\n<user_query>\ndo the thing\n</user_query>".
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
# Cursor redacts subagent reasoning/thinking with this literal — it can be a
# whole text block or trailing junk appended to a real response
# ("Done.\n\n[REDACTED]"), so we strip the token out rather than match blocks.
_REDACTED_RE = re.compile(r"\s*\[REDACTED\]\s*")


def _read_lines(path: str) -> list[dict[str, Any]]:
    """Return each non-empty JSON object from a transcript JSONL file.

    Opened as UTF-8 with ``errors="replace"`` so a stray byte can't abort the
    whole parse; non-dict JSON values are filtered out.

    Args:
        path: Transcript JSONL to read.

    Returns:
        Every line that decoded to a JSON object, in file order.
    """
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as transcript_file:
        for raw_line in transcript_file:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def _content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the message content blocks of a transcript line as a list.

    Cursor uses ``message.content`` as a list of typed blocks; tolerate a bare
    string (wrapped as a single text block) and missing content (empty list).

    Args:
        entry: One decoded transcript line.

    Returns:
        The line's typed content blocks; empty when there are none to read.
    """
    # The format is undocumented and every other shape here is skipped rather
    # than trusted, so a message that is not an object is skipped too. `or {}`
    # only covers a falsy message; a non-empty string passes straight through
    # and raises on .get, which escapes extract_subagent_conversation — its
    # caller catches OSError only — and costs the whole subagentStop capture.
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _user_prompt_text(entry: dict[str, Any]) -> str:
    """Extract the real prompt text from a user transcript line.

    Joins the line's text blocks, then unwraps ``<user_query>...</user_query>``
    if present (the opening prompt carries a ``<timestamp>`` prefix we drop).

    Args:
        entry: One decoded user transcript line.

    Returns:
        The prompt text, unwrapped from ``<user_query>`` when present.
    """
    # The text must be a string as well as present: the join below raises
    # TypeError on anything else, and a block is only required to be a dict.
    texts = [
        block.get("text", "")
        for block in _content_blocks(entry)
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    joined = "\n".join(text for text in texts if text).strip()
    match = _USER_QUERY_RE.search(joined)
    return (match.group(1).strip() if match else joined) or ""


def first_user_query(path: str) -> str:
    """Return the transcript's opening user query, or '' — used for matching.

    Lets the caller correlate a ``subagentStop`` hook (which knows the ``task``
    but not the child transcript path) to the right ``subagents/*.jsonl`` file.

    Args:
        path: Transcript JSONL to inspect.

    Returns:
        The opening user query, or '' when the transcript has none.
    """
    for entry in _read_lines(path):
        if entry.get("role") == "user":
            return _user_prompt_text(entry)
    return ""


def is_complete(path: str) -> bool:
    """Whether the transcript has been fully flushed (a ``turn_ended`` line).

    Cursor appends ``{"type": "turn_ended"}`` when the subagent finishes, so its
    presence means the final assistant response is on disk — used to bound the
    flush-race poll in ``extract_subagent_conversation``.

    Args:
        path: Transcript JSONL to inspect.

    Returns:
        True once a ``turn_ended`` line is present.
    """
    try:
        return any(entry.get("type") == "turn_ended" for entry in _read_lines(path))
    except OSError:
        return False


def latest_turn(path: str) -> dict[str, Any] | None:
    """Return the transcript's most recent turn (prompt, response, tool calls).

    Used by the no-stdin capture path (Cursor 3.18.x over remote/RDP delivers an
    empty stdin payload and only exposes the turn through
    ``CURSOR_TRANSCRIPT_PATH``). Reuses ``parse_transcript`` and returns the last
    turn so the collector can reconstruct the ``beforeSubmitPrompt.prompt``,
    ``afterAgentResponse.text``, and ``postToolUse`` fields the backend expects.

    Args:
        path: Transcript JSONL to read.

    Returns:
        ``{"user_prompt": str, "agent_response": str, "tool_calls": [...]}`` for
        the last turn, or ``None`` when the transcript is unreadable or has no
        turns. The transcript carries no tokens, model, or tool output, so those
        stay unrecoverable in this mode (``tool_output`` is always ``None``).
    """
    try:
        turns = parse_transcript(path).get("turns") or []
    except OSError:
        return None
    if not turns:
        return None
    last = turns[-1]
    return {
        "user_prompt": last.get("user_prompt") or "",
        "agent_response": last.get("agent_response") or "",
        "tool_calls": last.get("tool_calls") or [],
    }


def _empty_turn(user_prompt: str | None) -> dict[str, Any]:
    """Return a fresh child-turn accumulator seeded with ``user_prompt``.

    Args:
        user_prompt: Prompt text the new turn is seeded with.

    Returns:
        A fresh turn accumulator, including internal bookkeeping keys.
    """
    return {
        "user_prompt": user_prompt,
        "agent_response": None,
        "model": "",
        "response_id": "",
        "started_at": "",
        "ended_at": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "tool_calls": [],
        # Populated from an UpdateCurrentStep tool call as a response fallback.
        "_final_summary": "",
    }


def _finalize(turn: dict[str, Any]) -> dict[str, Any]:
    """Resolve the turn's final response and drop internal bookkeeping keys.

    Prefers the last non-redacted assistant text; falls back to an
    ``UpdateCurrentStep.final_summary`` when the subagent produced only a
    tool-driven completion with no trailing prose.

    Args:
        turn: Turn accumulator, mutated in place.

    Returns:
        The same turn, with its final response resolved and internal keys
        removed.
    """
    final_summary = turn.pop("_final_summary", "")
    if not turn.get("agent_response") and final_summary:
        turn["agent_response"] = final_summary
    return turn


def final_turn_is_closed(path: str) -> bool:
    """Whether the transcript's LAST turn has been fully written.

    :func:`is_complete` answers a different question — whether *any*
    ``turn_ended`` line is present. That is right for a subagent transcript,
    which holds one turn per file, and wrong for a session transcript: turn 1's
    marker would report every later turn as flushed, making a poll on it a no-op
    from turn 2 onward.

    The test is positional, not a count. Counting ``turn_ended`` against ``user``
    lines was measured wrong on real transcripts — a single turn can carry
    several ``user`` entries and still close with one marker — which under-reports
    a finished turn and costs a caller its whole poll budget on every hook. What
    actually distinguishes a finished turn is that nothing follows its marker.

    The final line is read raw rather than through :func:`_read_lines`, which
    drops anything that does not parse. That is right for extracting content and
    wrong here: a half-written line is the very evidence this looks for, and
    skipping it hands back the marker behind it, reporting a file that is
    actively being appended to as closed. Measured — a ``turn_ended`` followed
    by a torn line read as closed and rebuilt the previous turn.

    Known limit: a turn whose first line has not reached disk at all is still
    invisible — the previous turn's marker is genuinely last, so the file is
    closed by every test available here and that previous turn reads as the
    current one. Closing this needs a per-turn key, which Cursor does not expose
    in the no-stdin mode this serves.

    Args:
        path: Transcript JSONL to inspect.

    Returns:
        True when the last non-empty line is a complete ``turn_ended`` object.
        False when the file cannot be read, is empty, ends mid-turn, or ends on
        a line that does not parse — so a caller polling on this waits rather
        than reading a half-written turn.
    """
    last_raw_line = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as transcript_file:
            for raw_line in transcript_file:
                if raw_line.strip():
                    last_raw_line = raw_line
    except OSError:
        return False
    if not last_raw_line:
        return False
    try:
        last = json.loads(last_raw_line)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(last, dict) and last.get("type") == "turn_ended"


def parse_transcript(path: str) -> dict[str, Any]:
    """Parse a Cursor subagent transcript into ``{"turns": [...]}``.

    One turn per real user message (usually a single turn with many tool
    calls). Each turn is shaped for the API's child-session builder:
    ``user_prompt``, ``agent_response``, ``tool_calls`` (input only), zeroed
    token totals, and empty spans (Cursor exposes no per-message timestamps).

    The transcript is one JSON object per line, in two shapes::

        {"role": "user"|"assistant", "message": {"content": [block, ...]}}
        {"type": "turn_ended", "status": "success"|...}

    where a block is ``{"type": "text", "text": ...}`` or
    ``{"type": "tool_use", "name": ..., "input": {...}}``. This is not a
    documented format, so unknown line and block shapes are skipped rather
    than treated as errors.

    What the transcript does not contain bounds what any caller can report:
    no token usage, no tool output (a ``tool_use`` block carries the call
    input only), and no per-message timestamps.

    Args:
        path: Transcript JSONL to parse.

    Returns:
        ``{"turns": [...]}`` in the API's child-session shape.
    """
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for entry in _read_lines(path):
        role = entry.get("role")

        if role == "user":
            if current is not None:
                turns.append(_finalize(current))
            current = _empty_turn(_user_prompt_text(entry) or None)
            continue

        if role != "assistant":
            # turn_ended and any other bookkeeping lines carry no turn content.
            continue

        if current is None:
            # Assistant output before any user prompt — start an implicit turn.
            current = _empty_turn(None)

        for block in _content_blocks(entry):
            block_type = block.get("type")
            match block_type:
                case "text":
                    # A truthy non-string reaches re.sub and raises TypeError,
                    # which escapes to extract_subagent_conversation's blanket
                    # except and costs the whole subagentStop capture. `or ""`
                    # only covers the falsy case, so the type is checked here.
                    raw_text = block.get("text")
                    if not isinstance(raw_text, str):
                        continue
                    text = _REDACTED_RE.sub(" ", raw_text).strip()
                    if text:
                        # Last non-redacted assistant text wins as the response;
                        # earlier ones are intermediate narration.
                        current["agent_response"] = text
                case "tool_use":
                    tool_input = block.get("input")
                    if block.get("name") == "UpdateCurrentStep" and isinstance(
                        tool_input, dict
                    ):
                        summary = tool_input.get("final_summary")
                        if isinstance(summary, str) and summary:
                            current["_final_summary"] = summary
                    current["tool_calls"].append(
                        {
                            "started_at": "",
                            "tool_name": block.get("name", ""),
                            "tool_call_id": "",
                            "tool_input": tool_input,
                            "tool_output": None,
                        }
                    )

    if current is not None:
        turns.append(_finalize(current))

    return {"turns": turns}
