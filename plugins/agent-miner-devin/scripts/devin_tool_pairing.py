"""Give Devin CLI's PreToolUse/PostToolUse hooks a shared call id.

Claude Code and Copilot stamp every tool hook with a ``tool_use_id`` and the
Bloomfilter API pairs a tool's start and end hooks on it. Devin CLI's payloads
carry ``tool_name`` and ``tool_input`` but no per-call id, so the two hooks for
one execution are indistinguishable from the two hooks for two executions of
the same tool. This module issues the id itself: ``PreToolUse`` claims a fresh
one and parks it under the tool name; ``PostToolUse`` takes the oldest parked
id for that tool. In-order completion of same-named tools is therefore paired
exactly; when Devin runs two ``exec`` calls concurrently and they finish out
of order the outputs can swap, which is recorded on the envelope so the API
can weigh it.

State is one small JSON file per session in the batch directory, guarded by
the same advisory lock the batch file uses, because each hook is its own
process and Devin does run tool calls in parallel.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from typing import Any

from bloomfilter_common import _lock_file, debug_log, get_batch_dir

# Suffix of the per-session pairing state file, alongside ``<session>.jsonl``.
# Not ``.jsonl``, so the batch age sweep never mistakes it for a batch.
TOOL_STATE_SUFFIX = ".tools.json"

# Most ids parked per tool name. A PostToolUse that never fires (hook killed,
# tool aborted) leaves its id parked forever, and Devin's per-tool hooks fire
# thousands of times a session, so the queue is bounded; the oldest id is
# dropped when the bound is hit, which only misattributes an already-lost pair.
MAX_PENDING_PER_TOOL = 64

# Age at which an abandoned pairing file is removed by the SessionStart sweep.
TOOL_STATE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60


def _new_tool_call_id() -> str:
    """Mint an id that cannot collide with a runtime-issued one.

    Returns:
        ``devin-<32 hex chars>``.
    """
    return f"devin-{uuid.uuid4().hex}"


def _state_path(session_id: str) -> str:
    """Return the pairing state file for one session.

    Args:
        session_id: Devin's session id, already validated as a bare filename
            component by the caller's batch-file resolution.

    Returns:
        ``<batch-dir>/<session_id>.tools.json``.

    Raises:
        ValueError: If *session_id* is not a safe filename component.
    """
    if (
        not session_id
        or os.path.basename(session_id) != session_id
        or ".." in session_id
    ):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{session_id}{TOOL_STATE_SUFFIX}")


def _read_state(handle: Any) -> dict[str, list[str]]:
    """Decode the pending map from an open state file.

    Args:
        handle: A read/write text handle positioned anywhere.

    Returns:
        ``{tool_name: [oldest_id, ..., newest_id]}``; empty on any decode
        problem, since a corrupt state file must cost one turn's pairing, not
        the hook.
    """
    try:
        handle.seek(0)
        decoded = json.loads(handle.read() or "{}")
    except (OSError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        tool_name: [call_id for call_id in ids if isinstance(call_id, str)]
        for tool_name, ids in decoded.items()
        if isinstance(tool_name, str) and isinstance(ids, list)
    }


def _write_state(handle: Any, state: dict[str, list[str]]) -> None:
    """Replace the state file's contents.

    Args:
        handle: The same open handle :func:`_read_state` used.
        state: The pending map to persist. Tools with nothing pending are
            dropped so the file shrinks back as calls complete.
    """
    compact = {tool_name: ids for tool_name, ids in state.items() if ids}
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(compact, separators=(",", ":")))
    handle.flush()


@contextlib.contextmanager
def _locked_state(session_id: str):
    """Open the session's pairing file under an exclusive lock.

    Args:
        session_id: Session whose file is opened (created if absent).

    Yields:
        The open text handle, positioned at 0.
    """
    with open(_state_path(session_id), "a+", encoding="utf-8") as handle:
        with _lock_file(handle, exclusive=True):
            handle.seek(0)
            yield handle


def claim_tool_call_id(session_id: str, tool_name: str) -> str:
    """Mint and park an id for a tool that is about to run.

    Args:
        session_id: Session the PreToolUse hook belongs to.
        tool_name: The payload's ``tool_name``.

    Returns:
        The new id. Always returns one — when the state file cannot be used the
        id is still minted, so the start hook is attributable even though its
        end hook will not find it.
    """
    call_id = _new_tool_call_id()
    try:
        with _locked_state(session_id) as handle:
            state = _read_state(handle)
            pending = state.setdefault(tool_name, [])
            pending.append(call_id)
            if len(pending) > MAX_PENDING_PER_TOOL:
                del pending[: len(pending) - MAX_PENDING_PER_TOOL]
            _write_state(handle, state)
    except Exception as exception:
        debug_log(
            f"tool pairing: claim failed session_id={session_id} tool={tool_name} "
            f"type={type(exception).__name__} message={exception!s}"
        )
    return call_id


def resolve_tool_call_id(session_id: str, tool_name: str) -> tuple[str, bool]:
    """Take the oldest parked id for a tool that just finished.

    Args:
        session_id: Session the PostToolUse hook belongs to.
        tool_name: The payload's ``tool_name``.

    Returns:
        ``(call_id, paired)`` — the id to stamp on the end hook, and whether it
        came from a parked start. When nothing is parked a fresh id is minted so
        the end hook is still recorded, unpaired.
    """
    try:
        # No file means nothing was ever parked for this session (or Stop
        # already cleared it); opening it here would recreate an empty file
        # that only the age sweep would ever remove.
        if not os.path.exists(_state_path(session_id)):
            return _new_tool_call_id(), False
        with _locked_state(session_id) as handle:
            state = _read_state(handle)
            pending = state.get(tool_name) or []
            if pending:
                call_id = pending.pop(0)
                _write_state(handle, state)
                return call_id, True
    except Exception as exception:
        debug_log(
            f"tool pairing: resolve failed session_id={session_id} tool={tool_name} "
            f"type={type(exception).__name__} message={exception!s}"
        )
    return _new_tool_call_id(), False


def clear_tool_pairing(session_id: str) -> None:
    """Remove a session's pairing file.

    Called on ``Stop`` (every tool of the turn has ended, so anything still
    parked is an abandoned call) and on ``SessionEnd``.

    Args:
        session_id: Session whose file is removed. Missing is fine.
    """
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_state_path(session_id))
    except Exception as exception:
        debug_log(
            f"tool pairing: clear failed session_id={session_id} "
            f"type={type(exception).__name__} message={exception!s}"
        )


def sweep_stale_tool_state(max_age_seconds: int = TOOL_STATE_MAX_AGE_SECONDS) -> int:
    """Delete pairing files whose session died without a Stop or SessionEnd.

    Args:
        max_age_seconds: Age beyond which a file is removed.

    Returns:
        How many files were deleted.
    """
    try:
        batch_dir = get_batch_dir()
        names = os.listdir(batch_dir)
    except Exception:
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for name in names:
        if not name.endswith(TOOL_STATE_SUFFIX):
            continue
        path = os.path.join(batch_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed
