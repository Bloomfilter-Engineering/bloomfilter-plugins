"""Tests for thinking/reasoning extraction in the Claude Code plugin.

Claude Code writes each assistant content block (thinking, text, tool_use) on
its OWN transcript line and shares one message id across the lines of a single
response. These tests build transcripts in that exact shape and assert that
thinking is extracted with correct positions for both the main session
(``extract_transcript_summary``) and subagents (``_parse_subagent_transcript``).
"""

import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import bloomfilter_common as bc  # noqa: E402


def _write_transcript(tmp_path, lines):
    """Write a list of dict entries as JSONL and return the path.

    Args:
        tmp_path: pytest tmp_path fixture (a directory).
        lines: List of entry dicts to serialize one-per-line.

    Returns:
        str: Path to the written transcript file.
    """
    path = os.path.join(str(tmp_path), "transcript.jsonl")
    with open(path, "w") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def _assistant(msg_id, block, usage=True):
    """Build one assistant transcript entry carrying a single content block.

    Args:
        msg_id: The shared message id for the response.
        block: The single content block dict.
        usage: Whether to attach a usage dict (as Claude Code does).

    Returns:
        dict: An assistant transcript entry.
    """
    message = {"role": "assistant", "id": msg_id, "model": "claude-opus-4-8",
               "content": [block], "stop_reason": "tool_use"}
    if usage:
        message["usage"] = {"input_tokens": 10, "output_tokens": 5}
    return {"type": "assistant", "message": message, "timestamp": "2026-07-29T00:00:00Z"}


def _user(text):
    """Build a real user-prompt transcript entry."""
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": "2026-07-29T00:00:00Z"}


def test_main_turn_thinking_extracted_with_positions(tmp_path):
    """Thinking blocks are captured with position = preceding tool_use count.

    Layout mirrors reality: response A = thinking + tool_use (shared id),
    response B = thinking + text (shared id). The token logic dedups by id
    (keeping the last block per id), so extraction must walk raw entries or the
    thinking/text lines would be lost.
    """
    lines = [
        _user("do the thing"),
        _assistant("A", {"type": "thinking", "thinking": "first thought", "signature": "s1"}),
        _assistant("A", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        _assistant("B", {"type": "thinking", "thinking": "second thought", "signature": "s2"}),
        _assistant("B", {"type": "text", "text": "the answer"}),
    ]
    path = _write_transcript(tmp_path, lines)

    summary = bc.extract_transcript_summary(path)

    assert summary is not None
    assert "thinking" in summary
    assert summary["thinking"] == [
        {"content": "first thought", "position": 0},
        {"content": "second thought", "position": 1},
    ]


def test_main_turn_encrypted_thinking_emitted(tmp_path):
    """Signature-only thinking (empty text) is emitted as an encrypted marker.

    This is the dominant real-world case: current Claude Code (Opus extended
    thinking) persists thinking blocks with a signature but no readable text.
    We still surface that reasoning occurred, without any text.
    """
    lines = [
        _user("hi"),
        _assistant("A", {"type": "thinking", "thinking": "", "signature": "sig-only"}),
        _assistant("A", {"type": "text", "text": "hello"}),
    ]
    path = _write_transcript(tmp_path, lines)

    summary = bc.extract_transcript_summary(path)

    assert summary is not None
    assert summary["thinking"] == [{"position": 0, "encrypted": True}]


def test_main_turn_empty_thinking_without_signature_is_skipped(tmp_path):
    """A degenerate thinking block with neither text nor signature is dropped."""
    lines = [
        _user("hi"),
        _assistant("A", {"type": "thinking", "thinking": ""}),
        _assistant("A", {"type": "text", "text": "hello"}),
    ]
    path = _write_transcript(tmp_path, lines)

    summary = bc.extract_transcript_summary(path)

    assert "thinking" not in summary


def test_redacted_thinking_marked_encrypted(tmp_path):
    """redacted_thinking blocks become an encrypted marker on the timeline."""
    lines = [
        _user("hi"),
        _assistant("A", {"type": "redacted_thinking", "data": "xyz"}),
        _assistant("A", {"type": "text", "text": "done"}),
    ]
    path = _write_transcript(tmp_path, lines)

    summary = bc.extract_transcript_summary(path)

    assert summary["thinking"] == [{"position": 0, "encrypted": True}]


def test_subagent_thinking_interleaved(tmp_path):
    """Subagent transcript parsing captures thinking with tool-relative positions."""
    lines = [
        _user("explore"),
        _assistant("A", {"type": "thinking", "thinking": "look here", "signature": "s"}),
        _assistant("A", {"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}),
        _assistant("B", {"type": "thinking", "thinking": "now read", "signature": "s"}),
        _assistant("B", {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}),
        _assistant("C", {"type": "thinking", "thinking": "final", "signature": "s"}),
        _assistant("C", {"type": "text", "text": "found it"}),
    ]
    path = _write_transcript(tmp_path, lines)

    result = bc._parse_subagent_transcript(path)

    assert result is not None
    turn = result["turns"][0]
    assert turn["thinking"] == [
        {"content": "look here", "position": 0},
        {"content": "now read", "position": 1},
        {"content": "final", "position": 2},
    ]
    assert turn["agent_response"] == "found it"
    assert len(turn["tool_calls"]) == 2
