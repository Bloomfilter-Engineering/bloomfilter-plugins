"""Read Devin CLI's on-disk ATIF transcript for the data its hooks omit.

Devin CLI hook payloads carry no token counts, model id, request id or
timing. All of that lives in the session transcript the CLI writes to
``<data-dir>/transcripts/<session_id>.json`` in the Agent Trajectory
Interchange Format (ATIF): a root object with ``session_id``, ``agent`` and
``steps[]``, where each agent step records ``metadata.metrics`` (input, output,
cache-read and cache-creation tokens, time to first token), the concrete
``generation_model`` that served it, its ``request_id``, ``finish_reason`` and
its ``committed_acu_cost``. This module turns the trailing turn of that file
into the ``transcript_summary`` shape the Bloomfilter API already ingests from
the other collectors.

Everything here is best-effort and read-only: a missing, oversized or
malformed transcript yields ``None`` and the hook records what it has.
"""

from __future__ import annotations

import json
import os
import platform
from typing import Any

# Largest transcript this module will parse. A transcript is one JSON object and
# has to be read whole, so the bound is on bytes read rather than on a tail
# window. Devin compacts long sessions, and a run that grows past this is one
# where the token summary is the least of the concerns; skipping it keeps the
# Stop hook inside its timeout.
TRANSCRIPT_MAX_BYTES = 64_000_000

# Devin's default per-user data directory. The CLI migrated from
# ``~/.local/share/cognition`` to ``~/.local/share/devin`` and keeps a symlink at
# the old path, so only the new one is looked at. Overridable for tests and for
# installs that relocate XDG_DATA_HOME.
DATA_DIR_ENV_OVERRIDE = "BLOOMFILTER_DEVIN_DATA_DIR"

# Longest agent message kept as the turn's fallback response. Only consulted
# when the Stop payload carries no ``last_assistant_message``.
AGENT_RESPONSE_CAP = 20_000


def resolve_data_dir() -> str:
    """Return Devin CLI's data directory for this platform.

    Returns:
        The first existing candidate, or the platform default when none exists
        yet (so a caller can still build and log the path it looked for).
    """
    override = os.environ.get(DATA_DIR_ENV_OVERRIDE, "")
    if override and os.path.isabs(override):
        return override

    candidates: list[str] = []
    if platform.system() == "Windows":
        for variable in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(variable, "")
            if base and os.path.isabs(base):
                candidates.append(os.path.join(base, "devin", "cli"))
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", "")
        if xdg_data_home and os.path.isabs(xdg_data_home):
            candidates.append(os.path.join(xdg_data_home, "devin", "cli"))
        candidates.append(
            os.path.join(os.path.expanduser("~"), ".local", "share", "devin", "cli")
        )

    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[-1] if candidates else ""


def transcript_path_for(session_id: str) -> str:
    """Return the transcript file path for one session.

    Args:
        session_id: Devin's ``session_id`` from the hook payload. Used as a bare
            filename component only.

    Returns:
        ``<data-dir>/transcripts/<session_id>.json``, or an empty string when
        the id is not a safe filename component or no data dir resolves.
    """
    if (
        not session_id
        or os.path.basename(session_id) != session_id
        or ".." in session_id
    ):
        return ""
    data_dir = resolve_data_dir()
    if not data_dir:
        return ""
    return os.path.join(data_dir, "transcripts", f"{session_id}.json")


def load_transcript(transcript_path: str) -> dict[str, Any] | None:
    """Parse one ATIF transcript file.

    Args:
        transcript_path: Absolute path to the ``.json`` transcript.

    Returns:
        The decoded root object, or ``None`` when the file is missing, larger
        than :data:`TRANSCRIPT_MAX_BYTES`, unreadable, not JSON, or not an
        object carrying a ``steps`` list.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        if os.path.getsize(transcript_path) > TRANSCRIPT_MAX_BYTES:
            return None
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            decoded = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("steps"), list):
        return None
    return decoded


def _token_count(value: Any) -> int:
    """Coerce a token count read from the transcript.

    Args:
        value: Whatever the transcript held under a token key.

    Returns:
        A non-negative int; zero for anything that is not a finite number.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if (
        isinstance(value, float)
        and value == value
        and value not in (float("inf"), float("-inf"))
    ):
        return max(int(value), 0)
    return 0


def _is_user_step(step: dict[str, Any]) -> bool:
    """Whether a step is the user's prompt rather than agent output.

    Args:
        step: One entry of ``steps[]``.

    Returns:
        True for ``source == "user"`` or ``metadata.is_user_input``.
    """
    if step.get("source") == "user":
        return True
    metadata = step.get("metadata")
    return isinstance(metadata, dict) and bool(metadata.get("is_user_input"))


def _message_text(message: Any) -> str:
    """Flatten an ATIF ``message`` to plain text.

    Args:
        message: A string, or a list of content parts each carrying ``text``.

    Returns:
        The text, or an empty string for anything else.
    """
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for part in message:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _api_call_from_step(
    step: dict[str, Any], default_model: str
) -> dict[str, Any] | None:
    """Build one ``api_calls`` entry from an agent step.

    Devin nests its counts under ``metadata.metrics`` with the Anthropic-style
    names; the ATIF standard ``metrics`` object (``prompt_tokens`` /
    ``completion_tokens`` / ``cached_tokens``) is accepted as a fallback so an
    export that only carries the portable fields still yields counts.

    Args:
        step: One agent entry of ``steps[]``.
        default_model: Model to report when the step names none.

    Returns:
        The api_call dict, or ``None`` when the step carries no usage at all.
    """
    metadata = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
    metrics = (
        metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    )
    standard = step.get("metrics") if isinstance(step.get("metrics"), dict) else {}

    input_tokens = _token_count(metrics.get("input_tokens"))
    output_tokens = _token_count(metrics.get("output_tokens"))
    cache_read = _token_count(metrics.get("cache_read_tokens"))
    cache_creation = _token_count(metrics.get("cache_creation_tokens"))
    if not (input_tokens or output_tokens or cache_read or cache_creation):
        cached = _token_count(standard.get("cached_tokens"))
        prompt = _token_count(standard.get("prompt_tokens"))
        input_tokens = max(prompt - cached, 0)
        output_tokens = _token_count(standard.get("completion_tokens"))
        cache_read = cached
    if not (input_tokens or output_tokens or cache_read or cache_creation):
        return None

    model = metadata.get("generation_model") or step.get("model_name") or default_model
    api_call: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "model": model if isinstance(model, str) else "",
        "response_id": (
            metadata.get("request_id")
            if isinstance(metadata.get("request_id"), str)
            else ""
        ),
        "stop_reason": (
            metadata.get("finish_reason")
            if isinstance(metadata.get("finish_reason"), str)
            else ""
        ),
    }
    return api_call


def summarize_latest_turn(transcript: dict[str, Any]) -> dict[str, Any] | None:
    """Summarize the agent steps that follow the last user prompt.

    At ``Stop`` those steps are the turn that just finished. At the next
    ``UserPromptSubmit`` they are still the previous turn if Devin has not yet
    appended the new prompt, and nothing at all if it has — so the same
    function serves both the primary extraction and the backfill, and can
    never attribute one turn's usage to another.

    Args:
        transcript: A decoded ATIF root object from :func:`load_transcript`.

    Returns:
        ``{"api_calls": [...], "time_to_first_token_ms"?: int,
        "acu_cost"?: float, "agent_response"?: str}``, or ``None`` when the
        trailing turn holds no agent steps.
    """
    steps = [step for step in transcript.get("steps", []) if isinstance(step, dict)]
    last_user_index = -1
    for index, step in enumerate(steps):
        if _is_user_step(step):
            last_user_index = index
    turn_steps = [
        step for step in steps[last_user_index + 1 :] if step.get("source") == "agent"
    ]
    if not turn_steps:
        return None

    agent = transcript.get("agent") if isinstance(transcript.get("agent"), dict) else {}
    default_model = (
        agent.get("model_name") if isinstance(agent.get("model_name"), str) else ""
    )

    api_calls: list[dict[str, Any]] = []
    acu_cost = 0.0
    saw_acu = False
    time_to_first_token_ms: int | None = None
    agent_response = ""
    for step in turn_steps:
        api_call = _api_call_from_step(step, default_model)
        if api_call:
            api_calls.append(api_call)
        metadata = (
            step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
        )
        committed = metadata.get("committed_acu_cost")
        if isinstance(committed, (int, float)) and not isinstance(committed, bool):
            acu_cost += float(committed)
            saw_acu = True
        metrics = (
            metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
        )
        if time_to_first_token_ms is None and isinstance(
            metrics.get("ttft_ms"), (int, float)
        ):
            time_to_first_token_ms = int(metrics["ttft_ms"])
        text = _message_text(step.get("message")).strip()
        if text:
            agent_response = text

    summary: dict[str, Any] = {"api_calls": api_calls}
    if time_to_first_token_ms is not None:
        summary["time_to_first_token_ms"] = time_to_first_token_ms
    if saw_acu:
        summary["acu_cost"] = round(acu_cost, 6)
    if agent_response:
        summary["agent_response"] = agent_response[:AGENT_RESPONSE_CAP]
    return summary


def summarize_session_turn(session_id: str) -> dict[str, Any] | None:
    """Locate, load and summarize the trailing turn of one session's transcript.

    Args:
        session_id: Devin's ``session_id`` from the hook payload.

    Returns:
        The :func:`summarize_latest_turn` result, or ``None`` when the
        transcript is absent or unusable.
    """
    transcript = load_transcript(transcript_path_for(session_id))
    if transcript is None:
        return None
    return summarize_latest_turn(transcript)


def session_metadata(session_id: str) -> dict[str, str]:
    """Read the session-level agent identity from the transcript root.

    Args:
        session_id: Devin's ``session_id`` from the hook payload.

    Returns:
        ``{"cli_version": ..., "model": ...}`` with only the keys the transcript
        supplied; empty when the transcript is absent.
    """
    transcript = load_transcript(transcript_path_for(session_id))
    if transcript is None:
        return {}
    agent = transcript.get("agent") if isinstance(transcript.get("agent"), dict) else {}
    result: dict[str, str] = {}
    if isinstance(agent.get("version"), str) and agent["version"]:
        result["cli_version"] = agent["version"]
    if isinstance(agent.get("model_name"), str) and agent["model_name"]:
        result["model"] = agent["model_name"]
    return result
