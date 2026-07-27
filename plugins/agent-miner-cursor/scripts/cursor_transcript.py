"""Parse a Cursor subagent transcript into the backend's child-session shape.

Cursor runs each subagent as its own conversation and writes that conversation
to ``<parent_conv_dir>/subagents/<child_conv_id>.jsonl`` — but the
``subagentStop`` hook does NOT expose that path (``agent_transcript_path`` is
null) or the child conversation id. The plugin discovers the file by matching
the hook's ``task`` against each candidate transcript's opening user query (see
``bloomfilter_common.find_subagent_transcript``); this module turns the matched
file into ``{"turns": [...]}`` shaped for ``_build_child_turns``.

Transcript format (one JSON object per line):
    {"role": "user"|"assistant", "message": {"content": [block, ...]}}
    {"type": "turn_ended", "status": "success"|...}
where each ``block`` is ``{"type": "text", "text": ...}`` or
``{"type": "tool_use", "name": ..., "input": {...}}``.

Known limitations (Cursor exposes nothing more in the transcript):
  * No token usage — all token totals are 0.
  * ``tool_use`` blocks carry the call input but no output.
  * No per-message timestamps (only a human-readable ``<timestamp>`` inside the
    opening user text, which we do not parse); turn spans are left empty.
"""

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
    """
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _user_prompt_text(entry: dict[str, Any]) -> str:
    """Extract the real prompt text from a user transcript line.

    Joins the line's text blocks, then unwraps ``<user_query>...</user_query>``
    if present (the opening prompt carries a ``<timestamp>`` prefix we drop).
    """
    texts = [
        block.get("text", "")
        for block in _content_blocks(entry)
        if block.get("type") == "text" and block.get("text")
    ]
    joined = "\n".join(t for t in texts if t).strip()
    match = _USER_QUERY_RE.search(joined)
    return (match.group(1).strip() if match else joined) or ""


def first_user_query(path: str) -> str:
    """Return the transcript's opening user query, or '' — used for matching.

    Lets the caller correlate a ``subagentStop`` hook (which knows the ``task``
    but not the child transcript path) to the right ``subagents/*.jsonl`` file.
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
    """
    try:
        return any(entry.get("type") == "turn_ended" for entry in _read_lines(path))
    except OSError:
        return False


def _empty_turn(user_prompt: str | None) -> dict[str, Any]:
    """Return a fresh child-turn accumulator seeded with ``user_prompt``."""
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
    """
    final_summary = turn.pop("_final_summary", "")
    if not turn.get("agent_response") and final_summary:
        turn["agent_response"] = final_summary
    return turn


def parse_transcript(path: str) -> dict[str, Any]:
    """Parse a Cursor subagent transcript into ``{"turns": [...]}``.

    One turn per real user message (usually a single turn with many tool
    calls). Each turn is shaped for the backend's ``_build_child_turns``:
    ``user_prompt``, ``agent_response``, ``tool_calls`` (input only), zeroed
    token totals, and empty spans (Cursor exposes no per-message timestamps).
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
            if block_type == "text":
                text = _REDACTED_RE.sub(" ", block.get("text") or "").strip()
                if text:
                    # Last non-redacted assistant text wins as the response;
                    # earlier ones are intermediate narration.
                    current["agent_response"] = text
            elif block_type == "tool_use":
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
