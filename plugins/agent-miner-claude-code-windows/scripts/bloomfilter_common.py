import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PLUGIN_VERSION = "0.1.2"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_TAG = "claude-code-windows"  # disambiguates plugins sharing the same log dir


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_config_dir():
    """Return the Bloomfilter config directory for the current platform."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "bloomfilter")
    xdg = os.environ.get(
        "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
    )
    return os.path.join(xdg, "bloomfilter")


def secure_makedirs(path):
    """Create directories with owner-only permissions on Unix."""
    os.makedirs(path, exist_ok=True)
    if platform.system() != "Windows":
        os.chmod(path, stat.S_IRWXU)  # 0o700


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------


def _resolve_debug_log_dir():
    """Return the directory for debug.log.

    Always the bloomfilter config dir (%APPDATA%\\bloomfilter on Windows).
    Claude Code injects CLAUDE_PLUGIN_DATA pointing at a plugin-scoped cache
    dir, but we deliberately ignore it so debug.log lives next to the user's
    config.json and batches/ — one well-known place to look for diagnostics
    across all plugins.
    """
    return get_config_dir()


def debug_log(message):
    """Append a timestamped line to <bloomfilter-config>/debug.log.

    Silent on failure — the logger must never crash a hook.
    """
    try:
        log_dir = _resolve_debug_log_dir()
        secure_makedirs(log_dir)
        log_path = os.path.join(log_dir, DEBUG_LOG_NAME)
        timestamp = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        line = f"{timestamp} [{DEBUG_LOG_TAG}] {message}\n"
        with open(log_path, "a") as log_file:
            log_file.write(line)
        if platform.system() != "Windows":
            os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def read_json_config(path, key, default=""):
    """Safely read a single key from a JSON config file.

    Opens with utf-8-sig so a leading BOM is stripped — `Set-Content -Encoding
    UTF8` on Windows PowerShell 5.1 writes a BOM, and the README's Windows setup
    snippet uses exactly that, so user-created configs land here BOM-prefixed.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f).get(key, default) or default
    except Exception:
        return default


def bootstrap_config(plugin_root):
    """Copy the template config if the user config does not exist yet."""
    config_dir = get_config_dir()
    config_file = os.path.join(config_dir, "config.json")
    template = os.path.join(plugin_root, "bloomfilter.config.json")

    if not os.path.isfile(config_file):
        secure_makedirs(config_dir)
        shutil.copy2(template, config_file)
        print(
            f"[bloomfilter] Created config at {config_file} — add your API key to get started."
        )

    return config_file


def resolve_api_key():
    """Resolve the API key: env var > user config."""
    key = os.environ.get("BLOOMFILTER_API_KEY", "")
    if key:
        return key

    user_config = os.path.join(get_config_dir(), "config.json")
    return read_json_config(user_config, "api_key")


def resolve_api_url():
    """Resolve the API URL: env var > user config > default."""
    env_url = os.environ.get("BLOOMFILTER_URL", "")
    if env_url:
        return env_url

    user_config = os.path.join(get_config_dir(), "config.json")
    url = read_json_config(user_config, "url")
    if url:
        return url

    return DEFAULT_API_URL


# ---------------------------------------------------------------------------
# Payload / stdin
# ---------------------------------------------------------------------------


def read_payload():
    """Read JSON payload from stdin.

    Uses utf-8-sig on Windows so a leading BOM is stripped — PowerShell
    pipes to a native executable can prefix stdout with a UTF-8 BOM on
    Windows PowerShell 5.1, which would otherwise break json.loads.
    """
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8-sig")
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def get_git_branch(project_dir):
    """Return the current git branch, or '' on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Batch file helpers
# ---------------------------------------------------------------------------


def get_batch_dir():
    """Return (and create) the batch directory."""
    batch_dir = os.path.join(get_config_dir(), "batches")
    secure_makedirs(batch_dir)
    return batch_dir


def get_batch_file(session_id):
    """Return path to the JSONL batch file for *session_id*."""
    safe_id = os.path.basename(session_id)
    if not safe_id or safe_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_id}.jsonl")


def append_to_batch(session_id, entry):
    """Append a single JSON line to the batch file for *session_id*."""
    batch_file = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(batch_file, "a") as f:
        f.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def read_batch(session_id):
    """Read all entries from the batch file and return the list (no delete)."""
    batch_file = get_batch_file(session_id)
    if not os.path.isfile(batch_file):
        return []
    with open(batch_file, "r") as f:
        lines = f.readlines()
    entries = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def clear_batch(session_id):
    """Delete the batch file for *session_id*."""
    batch_file = get_batch_file(session_id)
    if os.path.isfile(batch_file):
        os.remove(batch_file)


def rewrite_batch(session_id, entries):
    """Re-write entries back to the batch file (used on upload failure)."""
    batch_file = get_batch_file(session_id)
    with open(batch_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


def upload_batch(api_url, api_key, payload):
    """POST raw hook batch to the Bloomfilter API. Returns True on 2xx.

    Validates the URL scheme up front: only http/https are allowed.

    Network interactions are logged to <bloomfilter-config>/debug.log: the
    request URL + session_id + hook count + payload bytes, the response
    status + truncated body, and any HTTPError / URLError / unexpected
    exception.
    """
    parsed = urllib.parse.urlparse(api_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return False

    url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    hook_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0

    try:
        data = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} error={type(exc).__name__}: {exc}"
        )
        return False

    debug_log(
        f"upload_batch: sending POST {url} session_id={session_id} "
        f"hooks={hook_count} bytes={len(data)}"
    )

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-MCP-Token": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
        debug_log(
            f"upload_batch: response status={status} session_id={session_id} "
            f"body={body[:500]!r}"
        )
        if status != 201:
            print(f"[bloomfilter] Upload response status: {status}", file=sys.stderr)
        return 200 <= status < 300
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            err_body = ""
        reason = getattr(exc, "reason", "")
        debug_log(
            f"upload_batch: HTTPError status={exc.code} reason={reason!r} "
            f"session_id={session_id} body={err_body[:500]!r}"
        )
        message = f"[bloomfilter] Upload failed with HTTP {exc.code}"
        if reason:
            message += f" {reason}"
        print(message, file=sys.stderr)
        if err_body:
            print(
                f"[bloomfilter] Upload response body: {err_body[:500]}", file=sys.stderr
            )
        return False
    except urllib.error.URLError as exc:
        debug_log(
            f"upload_batch: URLError session_id={session_id} reason={exc.reason!r}"
        )
        print(f"[bloomfilter] Upload failed: {exc.reason}", file=sys.stderr)
        return False
    except Exception as exc:
        debug_log(
            f"upload_batch: error session_id={session_id} "
            f"type={type(exc).__name__} message={exc!s}"
        )
        print(f"[bloomfilter] Upload failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utcnow_iso():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Token extraction (kept client-side — transcript is a local file)
# ---------------------------------------------------------------------------


def extract_transcript_summary(transcript_path):
    """Parse transcript JSONL and return a condensed token summary.

    Returns a dict with an ``api_calls`` list, or None on failure.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None

    try:
        file_size = os.path.getsize(transcript_path)
        read_start = max(0, file_size - 100_000)
        with open(transcript_path, "rb") as tf:
            tf.seek(read_start)
            raw = tf.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()

        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Find the last real user prompt (not a tool_result)
        last_user_idx = -1
        for i, entry in enumerate(entries):
            if entry.get("type") != "user":
                continue
            if entry.get("toolUseResult"):
                continue
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list) and all(
                c.get("type") == "tool_result" for c in content
            ):
                continue
            last_user_idx = i

        # Collect all assistant entries in the current turn
        turn_entries = entries[last_user_idx + 1 :] if last_user_idx >= 0 else entries
        all_assistant = [
            e
            for e in turn_entries
            if (
                e.get("type") == "assistant"
                or e.get("message", {}).get("role") == "assistant"
            )
            and e.get("message", {}).get("usage")
        ]

        if not all_assistant:
            return None

        # Deduplicate by response_id (streaming produces multiple entries)
        seen = {}
        for e in all_assistant:
            rid = e.get("message", {}).get("id", "")
            seen[rid] = e
        assistant_entries = list(seen.values())

        if not assistant_entries:
            return None

        api_calls = []
        for entry in assistant_entries:
            message = entry.get("message", {})
            usage = message.get("usage", {})
            api_call = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                "model": message.get("model", ""),
                "response_id": message.get("id", ""),
                "stop_reason": message.get("stop_reason", ""),
            }
            speed = usage.get("speed")
            if speed:
                api_call["speed"] = speed
            api_calls.append(api_call)

        return {"api_calls": api_calls}

    except Exception:
        return None


# Cap a single tool_output/text payload so a subagent that read large files
# doesn't bloat the batch upload. Generous enough to keep summaries intact.
_SUBAGENT_FIELD_CAP = 10_000


def _cap_text(value):
    """Truncate a string value to the subagent field cap; pass through others."""
    if isinstance(value, str) and len(value) > _SUBAGENT_FIELD_CAP:
        return value[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
    return value


def extract_subagent_conversation(
    agent_transcript_path,
    expected_last_message=None,
    max_wait_s=2.0,
    poll_s=0.1,
):
    """Parse a subagent transcript, waiting for it to finish flushing.

    Claude Code fires ``SubagentStop`` before it has necessarily flushed the
    subagent's FINAL assistant message to the transcript file — a race that
    otherwise captures a partial turn (thinking only, tiny token counts, empty
    response). ``expected_last_message`` is the SubagentStop payload's
    ``last_assistant_message`` (authoritative + complete); we poll the transcript
    (bounded by ``max_wait_s``) until its last assistant text matches, then
    backfill the final response from it if the file still hasn't caught up.

    Args:
        agent_transcript_path: Path to the subagent (sidechain) transcript.
        expected_last_message: The subagent's final message per the hook payload.
        max_wait_s: Max seconds to wait for the transcript to flush.
        poll_s: Poll interval while waiting.

    Returns:
        ``{"turns": [...]}`` (see _parse_subagent_transcript) or None.
    """
    if not agent_transcript_path or not os.path.exists(agent_transcript_path):
        return None

    expected = (expected_last_message or "").strip()
    expected_capped = (_cap_text(expected) or "").strip()
    deadline = time.monotonic() + max_wait_s
    result = None
    matched = False
    while True:
        result = _parse_subagent_transcript(agent_transcript_path)
        if not expected:
            break
        last_ar = ""
        if result and result.get("turns"):
            last_ar = (result["turns"][-1].get("agent_response") or "").strip()
        # Caught up only on a complete match against the capped expected message.
        matched = bool(last_ar) and last_ar == expected_capped
        if matched or time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    # If we never confirmed a complete match, the transcript's final response is
    # missing OR partially flushed — replace it with the authoritative message so
    # a partial (non-empty) capture can't survive.
    if result and expected and not matched and result.get("turns"):
        result["turns"][-1]["agent_response"] = _cap_text(expected_last_message)
    return result


def _parse_subagent_transcript(agent_transcript_path):
    """Parse a subagent (sidechain) transcript into structured turns.

    Unlike :func:`extract_transcript_summary` (which reads only the tail and
    returns token totals for the last turn), this reads the WHOLE subagent
    transcript and returns per-turn user_prompt/agent_response, tool calls, and
    summed token usage so the backend can build a full child AgentSession.

    A subagent transcript is the same JSONL format as a normal session and its
    first user entry is the real task prompt. Normally there is a single real
    user prompt (one turn with many tool calls), but this splits on every real
    user prompt to stay faithful if a subagent had multiple.

    Returns ``{"turns": [ {user_prompt, agent_response, tool_calls, model,
    response_id, input_tokens, output_tokens, cache_read_tokens,
    cache_creation_tokens, started_at, ended_at} ]}`` or None on failure/empty.
    """
    if not agent_transcript_path or not os.path.exists(agent_transcript_path):
        return None

    try:
        with open(agent_transcript_path, "rb") as tf:
            raw = tf.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()

        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        def _is_real_user_prompt(entry):
            if entry.get("type") != "user":
                return False
            if entry.get("toolUseResult"):
                return False
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, list) and all(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in content
            ):
                return False
            return True

        def _user_text(entry):
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                return "\n".join(p for p in parts if p)
            return ""

        turns = []
        current = None

        def _finalize(turn):
            # Dedup assistant usage by response_id (streaming emits duplicates).
            usage_by_id = turn.pop("_usage_by_id", {})
            totals = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
            for usage in usage_by_id.values():
                totals["input_tokens"] += usage.get("input_tokens", 0)
                totals["output_tokens"] += usage.get("output_tokens", 0)
                totals["cache_read_tokens"] += usage.get(
                    "cache_read_input_tokens", 0
                )
                totals["cache_creation_tokens"] += usage.get(
                    "cache_creation_input_tokens", 0
                )
            turn.update(totals)
            turn["tool_calls"] = list(turn.pop("_tool_calls_by_id", {}).values())
            return turn

        for entry in entries:
            etype = entry.get("type")
            msg = entry.get("message", {})
            ts = entry.get("timestamp")

            if _is_real_user_prompt(entry):
                if current is not None:
                    turns.append(_finalize(current))
                current = {
                    "user_prompt": _cap_text(_user_text(entry)),
                    "agent_response": None,
                    "model": "",
                    "response_id": "",
                    "started_at": ts,
                    "ended_at": ts,
                    "_usage_by_id": {},
                    "_tool_calls_by_id": {},
                }
                continue

            if current is None:
                # Tool activity before any real prompt — start an implicit turn.
                current = {
                    "user_prompt": None,
                    "agent_response": None,
                    "model": "",
                    "response_id": "",
                    "started_at": ts,
                    "ended_at": ts,
                    "_usage_by_id": {},
                    "_tool_calls_by_id": {},
                }

            if ts:
                current["ended_at"] = ts

            is_assistant = etype == "assistant" or msg.get("role") == "assistant"
            if is_assistant:
                if msg.get("usage"):
                    rid = msg.get("id", "")
                    current["_usage_by_id"][rid] = msg["usage"]
                    if msg.get("model"):
                        current["model"] = msg["model"]
                    if rid:
                        current["response_id"] = rid
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text" and block.get("text"):
                            current["agent_response"] = _cap_text(block["text"])
                        elif btype == "tool_use":
                            current["_tool_calls_by_id"][block.get("id", "")] = {
                                "tool_name": block.get("name", ""),
                                "tool_input": block.get("input"),
                                "tool_output": None,
                                "tool_call_id": block.get("id", ""),
                                "started_at": ts,
                            }
                elif isinstance(content, str) and content:
                    current["agent_response"] = _cap_text(content)
            else:
                # user tool_result entries — attach output to the matching call.
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                        ):
                            call = current["_tool_calls_by_id"].get(
                                block.get("tool_use_id", "")
                            )
                            if call is not None:
                                call["tool_output"] = _cap_text(
                                    _stringify_tool_result(block.get("content"))
                                )

        if current is not None:
            turns.append(_finalize(current))

        if not turns:
            return None

        return {"turns": turns}

    except Exception:
        return None


def _stringify_tool_result(content):
    """Flatten a tool_result content field (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", "") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""
