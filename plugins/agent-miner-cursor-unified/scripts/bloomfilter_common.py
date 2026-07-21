from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
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
from typing import IO, Any, Callable, Iterator

# Platform-specific stdlib modules used by ``_lock_file`` below.
if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.1.3"
_SUBAGENT_FIELD_CAP = 10_000
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_MAX_BYTES = 1_000_000  # 1 MB — rotation cap per file
DEBUG_LOG_BACKUP_COUNT = 1  # keep one rotated backup → ~2 MB max on disk
DEBUG_LOG_TAG = "cursor-unified"  # disambiguates plugins sharing the same log dir

_debug_logger = None  # Lazy-init singleton; populated on first debug_log() call.


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_config_dir() -> str:
    """Return the Bloomfilter config directory for the current platform."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "bloomfilter")
    xdg = os.environ.get(
        "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
    )
    return os.path.join(xdg, "bloomfilter")


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------


def _resolve_debug_log_dir() -> str:
    """Return the directory for debug.log.

    Cursor / Claude / Codex inject a plugin data dir env var when present.
    Fall back to the bloomfilter config dir so the log lives next to the
    user's batches and config.json (%APPDATA%\\bloomfilter on Windows).
    """
    return (
        os.environ.get("PLUGIN_DATA")
        or os.environ.get("CURSOR_PLUGIN_DATA")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or get_config_dir()
    )


def _build_debug_logger() -> logging.Logger:
    """Construct the private debug logger backed by RotatingFileHandler.

    Uses a dedicated logger name with propagate=False so it cannot affect
    (or be affected by) other code that uses the stdlib logging module.
    """
    log_dir = _resolve_debug_log_dir()
    secure_makedirs(log_dir)
    log_path = os.path.join(log_dir, DEBUG_LOG_NAME)

    logger = logging.getLogger("bloomfilter.debug")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # Idempotent: avoid stacking handlers if this is somehow called twice
    # in the same process.
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=DEBUG_LOG_MAX_BYTES,
            backupCount=DEBUG_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            fmt=f"%(asctime)s.%(msecs)03dZ [{DEBUG_LOG_TAG}] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime  # UTC timestamps
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def debug_log(message: str) -> None:
    """Append a timestamped line to <plugin-data>/debug.log.

    Backed by ``logging.handlers.RotatingFileHandler``: 1 MB per file with one
    rotated backup, so disk usage is capped at ~2 MB. Silent on failure — the
    logger must never crash a hook.
    """
    global _debug_logger
    try:
        if _debug_logger is None:
            _debug_logger = _build_debug_logger()
        _debug_logger.info(message)
    except Exception:
        pass


def secure_makedirs(path: str) -> None:
    """Create directories with owner-only permissions on Unix."""
    os.makedirs(path, exist_ok=True)
    if platform.system() != "Windows":
        os.chmod(path, stat.S_IRWXU)  # 0o700


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def read_json_config(path: str, key: str, default: str = "") -> str:
    """Safely read a single key from a JSON config file.

    Opens with utf-8-sig so a leading BOM is stripped — `Set-Content -Encoding
    UTF8` on Windows PowerShell 5.1 writes a BOM, and the README's setup snippet
    uses exactly that, so user-created configs land here BOM-prefixed.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f).get(key, default) or default
    except Exception:
        return default


def bootstrap_config(plugin_root: str) -> str:
    """Copy the template config if the user config does not exist yet."""
    config_dir = get_config_dir()
    config_file = os.path.join(config_dir, "config.json")
    template = os.path.join(plugin_root, "bloomfilter.config.json")

    if not os.path.isfile(config_file):
        secure_makedirs(config_dir)
        shutil.copy2(template, config_file)
        if platform.system() != "Windows":
            os.chmod(config_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        print(
            f"[bloomfilter] Created config at {config_file} — add your API key to get started.",
            file=sys.stderr,
        )

    return config_file


def resolve_api_key() -> str:
    """Resolve the API key: BLOOMFILTER_API_KEY env var > user config.

    Project-level config is intentionally NOT consulted for the API key —
    project configs live in the repo and can be accidentally committed.
    The user config (~/.config/bloomfilter/config.json) and the env var
    are the only supported places to store the API key.
    """
    key = os.environ.get("BLOOMFILTER_API_KEY", "")
    if key:
        return key

    user_config = os.path.join(get_config_dir(), "config.json")
    return read_json_config(user_config, "api_key")


def resolve_api_url() -> str:
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


def read_payload() -> Any:
    """Read JSON payload from stdin.

    Returns the parsed JSON value — normally a dict, but any JSON type is
    possible, so callers must validate the shape (the collect hook checks
    ``isinstance(payload, dict)``). Returns ``{}`` for empty or non-JSON input.
    """
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8")
    raw = sys.stdin.read().lstrip("\ufeff")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("[bloomfilter] Ignoring non-JSON hook payload.", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def _resolve_git_executable() -> str:
    """Return a git executable path if available, or '' if none is found."""
    git = shutil.which("git")
    if git:
        return git

    if platform.system() != "Windows":
        return ""

    candidates = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(os.path.join(base, "Git", "cmd", "git.exe"))
            candidates.append(os.path.join(base, "Git", "bin", "git.exe"))
    local_app_data = os.environ.get("LocalAppData")
    if local_app_data:
        candidates.append(
            os.path.join(local_app_data, "Programs", "Git", "cmd", "git.exe")
        )
        candidates.append(
            os.path.join(local_app_data, "Programs", "Git", "bin", "git.exe")
        )
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate):
            return candidate

    return ""


def get_git_branch(project_dir: str) -> str:
    """Return the current git branch, or '' on failure."""
    git = _resolve_git_executable()
    if not git:
        return ""

    try:
        result = subprocess.run(
            [git, "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Batch file helpers (with file locking for concurrent hook processes)
# ---------------------------------------------------------------------------


if platform.system() != "Windows":

    @contextlib.contextmanager
    def _lock_file(fp: IO, exclusive: bool = True) -> Iterator[None]:
        """Acquire an flock on an open file, release on exit."""
        op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fp, op)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)

else:

    @contextlib.contextmanager
    def _lock_file(fp: IO, exclusive: bool = True) -> Iterator[None]:
        """Cross-process byte-range lock on Windows via ``msvcrt.locking``.

        msvcrt only supports exclusive locks — the ``exclusive`` arg is
        accepted for API parity with the POSIX implementation but ignored.
        Locks 1 byte at offset 0 as a coordination token. ``LK_LOCK``
        retries every second up to 10 times before raising; if it does
        raise we proceed unlocked (better than crashing the hook).

        File position is saved and restored so the lock's seek to offset 0
        does not disturb append-mode writes.
        """
        try:
            fp.flush()
        except (OSError, ValueError):
            pass
        try:
            pos = fp.tell()
        except (OSError, ValueError):
            pos = None

        try:
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            print(
                f"[bloomfilter] Could not acquire batch file lock ({exc}); "
                "proceeding unsynchronized.",
                file=sys.stderr,
            )
            if pos is not None:
                try:
                    fp.seek(pos)
                except (OSError, ValueError):
                    pass
            yield
            return

        try:
            if pos is not None:
                fp.seek(pos)
            yield
        finally:
            try:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            if pos is not None:
                try:
                    fp.seek(pos)
                except (OSError, ValueError):
                    pass


def get_batch_dir() -> str:
    """Return (and create) the batch directory."""
    batch_dir = os.path.join(get_config_dir(), "batches")
    secure_makedirs(batch_dir)
    return batch_dir


def get_batch_file(session_id: str) -> str:
    """Return path to the JSONL batch file for *session_id*."""
    safe_id = os.path.basename(session_id)
    if not safe_id or safe_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_id}.jsonl")


def append_to_batch(session_id: str, entry: dict) -> None:
    """Append a single JSON line to the batch file for *session_id*."""
    batch_file = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(batch_file, "a") as f:
        with _lock_file(f, exclusive=True):
            f.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def append_to_batch_deduped(
    session_id: str, entry: dict, is_duplicate: Callable[[list], bool]
) -> bool:
    """Append *entry* unless *is_duplicate* judges it already batched.

    ``is_duplicate`` receives the batch's existing records (as ``read_batch``
    returns them, in order) and returns True to skip the append. The existence
    check and the append run under a single exclusive lock, so two
    near-simultaneous hook processes cannot both pass the check and double-write:
    Cursor fires some hooks (notably ``afterAgentThought``) more than once for a
    single event, microseconds apart in separate processes, so a non-atomic
    check-then-append would race. Returns True if appended, False if skipped.
    """
    batch_file = get_batch_file(session_id)
    with open(batch_file, "a+") as f:
        with _lock_file(f, exclusive=True):
            f.seek(0)
            existing = []
            for line in f.readlines():
                is_record, value = _decode_batch_line(line)
                if is_record:
                    existing.append(value)
            if is_duplicate(existing):
                return False
            # 'a+' append mode writes at EOF regardless of the read seek above.
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            # Flush before releasing the lock: f.write only buffers in Python,
            # and the lock is released at the end of this block while the file
            # is not closed (flushed) until the outer 'with' exits. Without this
            # the next process could take the lock, reread, miss the append, and
            # write the duplicate anyway — defeating the dedup.
            f.flush()
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    return True


def _decode_batch_line(line: str) -> tuple[bool, Any]:
    """Decode one raw JSONL batch line.

    Returns ``(is_record, value)``. ``is_record`` is True only for a non-blank
    line that parses as JSON — exactly the lines ``read_batch`` returns and
    ``upload_batch`` sends — and False for a blank or corrupt line. ``value``
    holds the decoded object when ``is_record`` is True, else ``None``.

    Both ``read_batch`` and ``drop_leading_entries`` route through this so the
    records uploaded and the records drained can never diverge: corrupt lines
    are skipped identically on both sides.
    """
    stripped = line.strip()
    if not stripped:
        return False, None
    try:
        return True, json.loads(stripped)
    except json.JSONDecodeError:
        return False, None


def read_batch(session_id: str) -> list[dict]:
    """Read all entries from the batch file and return the list (no delete)."""
    batch_file = get_batch_file(session_id)
    if not os.path.isfile(batch_file):
        return []
    with open(batch_file, "r") as f:
        with _lock_file(f, exclusive=False):
            lines = f.readlines()
    entries = []
    for line in lines:
        is_record, value = _decode_batch_line(line)
        if is_record:
            entries.append(value)
    return entries


def rewrite_batch(session_id: str, entries: list[dict]) -> None:
    """Re-write entries back to the batch file (race-safe).

    Opens with ``a+`` so the file is not truncated until *after* the
    exclusive lock is acquired. Concurrent ``append_to_batch`` calls
    block on the same lock and never lose a line.
    """
    batch_file = get_batch_file(session_id)
    with open(batch_file, "a+") as f:
        with _lock_file(f, exclusive=True):
            f.seek(0)
            f.truncate()
            for entry in entries:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def clear_batch(session_id: str) -> None:
    """Truncate the batch file for *session_id* (race-safe).

    Delegates to ``rewrite_batch`` so the truncation is performed while
    holding the exclusive lock. Leaves a zero-byte file rather than
    deleting; ``read_batch`` returns ``[]`` for both cases.
    """
    rewrite_batch(session_id, [])


def drop_leading_entries(session_id: str, count: int) -> None:
    """Remove the first *count* entries from the batch file (race-safe).

    Used after a successful upload to delete exactly the entries that were
    uploaded while preserving any entries ``append_to_batch`` added
    concurrently — those land after the uploaded snapshot, so they survive as
    the trailing lines here. This is the safe alternative to ``clear_batch``,
    which would truncate those concurrent appends away.

    The read-modify-write happens under a single exclusive lock (``a+`` so the
    file is not truncated until the lock is held), so a concurrent append
    either completes before this runs (and is preserved) or blocks until after.

    Counts only the valid JSON records ``read_batch`` would return (via
    ``_decode_batch_line``), so corrupt or blank lines in the leading region are
    discarded without consuming the drop count — otherwise a corrupt line could
    leave an already-uploaded entry behind to be re-sent next batch.
    """
    if count <= 0:
        return
    batch_file = get_batch_file(session_id)
    if not os.path.isfile(batch_file):
        return
    with open(batch_file, "a+") as f:
        with _lock_file(f, exclusive=True):
            f.seek(0)
            lines = f.readlines()
            kept = []
            dropped = 0
            for line in lines:
                if dropped < count:
                    is_record, _ = _decode_batch_line(line)
                    if is_record:
                        dropped += 1
                    continue
                kept.append(line)
            f.seek(0)
            f.truncate()
            f.writelines(kept)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


def _sanitize_url_for_log(url: str) -> str:
    """Return scheme://host[:port]/path — drops userinfo, query, and fragment.

    debug.log is user-local but lives next to config.json; sanitization keeps
    embedded credentials or signed query params out of the rotating log.
    """
    parts = urllib.parse.urlsplit(url or "")
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def upload_batch(api_url: str, api_key: str, payload: dict) -> bool:
    """POST raw hook batch to the Bloomfilter API. Returns True on success.

    Validates the URL scheme up front: only http/https are allowed. Other
    schemes (file://, ftp://, gopher://, ...) would otherwise be honoured
    by urllib.request.urlopen if a local config supplies a malicious url.

    Network interactions are logged to <plugin-data>/debug.log: the sanitized
    request URL + session_id + hook count, the response status (and body
    length on HTTPError), and any HTTPError / URLError / unexpected exception.
    """
    parsed = urllib.parse.urlparse(api_url or "")

    # Accessing .port raises ValueError for malformed ports (non-numeric or out
    # of range). Validate it here so a bad config URL is rejected cleanly rather
    # than throwing later in _sanitize_url_for_log, which runs before the upload
    # try/except below.
    try:
        parsed.port  # noqa: B018 — evaluated for its validation side effect
        port_ok = True
    except ValueError:
        port_ok = False

    if parsed.scheme not in ("http", "https") or not parsed.netloc or not port_ok:
        debug_log(
            "upload_batch: skipped — invalid api_url "
            f"scheme={parsed.scheme!r} netloc={parsed.netloc!r}"
        )
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return False

    # Never send the API token (X-MCP-Token) over cleartext HTTP. Allow http
    # only for loopback hosts to keep local development working.
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        debug_log("upload_batch: skipped — refusing cleartext (non-loopback) API URL")
        print(
            "[bloomfilter] Upload skipped: Bloomfilter API URL must use HTTPS.",
            file=sys.stderr,
        )
        return False

    full_url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    safe_url = _sanitize_url_for_log(full_url)
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    raw_hooks = payload.get("hooks", []) if isinstance(payload, dict) else []
    hook_count = len(raw_hooks) if isinstance(raw_hooks, (list, tuple)) else 0

    try:
        data = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} error={type(exc).__name__}: {exc}"
        )
        return False

    debug_log(
        f"upload_batch: sending POST {safe_url} session_id={session_id} "
        f"hooks={hook_count} bytes={len(data)}"
    )

    try:
        req = urllib.request.Request(
            full_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-MCP-Token": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
        debug_log(f"upload_batch: response status={status} session_id={session_id}")
        if status != 201:
            print(f"[bloomfilter] Upload response status: {status}", file=sys.stderr)
        return 200 <= status < 300
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        reason = getattr(exc, "reason", "")
        debug_log(
            f"upload_batch: HTTPError status={exc.code} reason={reason!r} "
            f"session_id={session_id} body_chars={len(body)}"
        )
        message = f"[bloomfilter] Upload failed with HTTP {exc.code}"
        if reason:
            message += f" {reason}"
        print(message, file=sys.stderr)
        if body:
            print(f"[bloomfilter] Upload response body: {body[:500]}", file=sys.stderr)
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


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Subagent transcript capture
# ---------------------------------------------------------------------------


def _cap_text(value: Any) -> Any:
    """Truncate an over-long string field; pass non-strings through unchanged.

    Subagent transcripts can carry very large tool inputs / responses. Cap them
    so a single batch upload stays bounded, mirroring the Codex/Claude Code
    plugins and the backend's field expectations.
    """
    if not isinstance(value, str):
        return value
    if len(value) > _SUBAGENT_FIELD_CAP:
        return value[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
    return value


def _cap_conversation(conversation: dict[str, Any]) -> None:
    """Cap the free-text fields of a parsed subagent conversation in place.

    Caps ``user_prompt``, ``agent_response``, and each tool call's
    ``tool_output``. Tool outputs merged from the stray batch are often dicts,
    so oversized non-string outputs are serialized and truncated to keep the
    upload bounded. ``tool_input`` is left raw (matches the main-session
    ToolCall shape).
    """
    for turn in conversation.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        if turn.get("user_prompt") is not None:
            turn["user_prompt"] = _cap_text(turn["user_prompt"])
        if turn.get("agent_response") is not None:
            turn["agent_response"] = _cap_text(turn["agent_response"])
        for tool_call in turn.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            output = tool_call.get("tool_output")
            if isinstance(output, str):
                tool_call["tool_output"] = _cap_text(output)
            elif output is not None:
                # Non-string (dict/list) output: cap by serialized size so a
                # large tool result can't blow up the batch. Only rewritten when
                # it actually exceeds the cap, so small dicts stay dicts.
                try:
                    serialized = json.dumps(output)
                except (TypeError, ValueError):
                    serialized = str(output)
                if len(serialized) > _SUBAGENT_FIELD_CAP:
                    tool_call["tool_output"] = (
                        serialized[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
                    )


def find_subagent_transcript(parent_transcript_path: str, task: str) -> str | None:
    """Locate a Cursor subagent's own transcript file for a ``subagentStop``.

    Cursor writes each subagent conversation to
    ``<parent_conv_dir>/subagents/<child_conv_id>.jsonl`` but the hook exposes
    neither that path (``agent_transcript_path`` is null) nor the child
    conversation id. It DOES give the parent transcript path and the subagent's
    ``task``, so we scan the sibling ``subagents/`` dir and return the file
    whose opening user query matches the task.

    Args:
        parent_transcript_path: ``payload.transcript_path`` (the parent
            conversation's transcript), used to locate the ``subagents/`` dir.
        task: ``payload.task`` — the subagent's prompt, matched against each
            candidate's first user query.

    Returns:
        Absolute path to the matching transcript, or None if the dir/file is
        missing or nothing matches.
    """
    if not parent_transcript_path or not task:
        return None
    parent_dir = os.path.dirname(parent_transcript_path)
    subagents_dir = os.path.join(parent_dir, "subagents")
    if not os.path.isdir(subagents_dir):
        return None

    # Local import: cursor_transcript lives beside this module on sys.path (the
    # hook entrypoint inserts the scripts dir before importing).
    from cursor_transcript import first_user_query

    wanted = task.strip()
    candidates = sorted(
        os.path.join(subagents_dir, name)
        for name in os.listdir(subagents_dir)
        if name.endswith(".jsonl")
    )
    for candidate in candidates:
        try:
            if first_user_query(candidate).strip() == wanted:
                return candidate
        except OSError:
            continue
    # Exactly one subagent this turn and no text match (e.g. the task was
    # reformatted): fall back to the sole candidate rather than losing it.
    if len(candidates) == 1:
        return candidates[0]
    return None


def _read_child_batch(
    child_conv_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return a subagent's tool calls and thinking from its stray batch.

    A Cursor subagent runs as its own conversation whose live hooks
    (``postToolUse``, ``afterAgentThought``, …) land in
    ``batches/<child_conv_id>.jsonl`` but never upload — no session/turn/response
    hooks fire for a child conversation, so the batch just orphans. It is the
    ONLY place the subagent's tool OUTPUTS and (unredacted) THINKING exist; the
    transcript records tool inputs only and no thinking.

    Returns ``(tool_calls, thinkings)`` where:
      * ``tool_calls`` = ordered ``postToolUse`` as ``[{tool_name, tool_input,
        tool_output, tool_call_id}]``.
      * ``thinkings`` = ordered ``afterAgentThought`` as ``[{content,
        preceding_tools}]`` where ``preceding_tools`` is the number of
        ``postToolUse`` seen before the thought — used to interleave it back into
        the transcript's tool sequence.
    Both empty on any error or if the batch is absent.
    """
    try:
        entries = read_batch(child_conv_id)
    except Exception:
        return [], []
    tool_calls: list[dict[str, Any]] = []
    thinkings: list[dict[str, Any]] = []
    for entry in entries:
        hook = entry.get("hook_event_name")
        payload = entry.get("payload") or {}
        if hook == "postToolUse":
            tool_calls.append(
                {
                    "tool_name": payload.get("tool_name", ""),
                    "tool_input": payload.get("tool_input"),
                    "tool_output": payload.get("tool_output"),
                    "tool_call_id": payload.get("tool_use_id", ""),
                }
            )
        elif hook == "afterAgentThought":
            text = payload.get("text")
            if text:
                thinkings.append({"content": text, "preceding_tools": len(tool_calls)})
    return tool_calls, thinkings


def _attach_thinking(
    conversation: dict[str, Any],
    thinkings: list[dict[str, Any]],
    batch_tool_names: set[str],
) -> None:
    """Interleave the subagent's thinking into a turn's tool sequence, in place.

    Cursor's transcript carries no thinking (not even a redacted marker), so the
    only ordering signal is each thought's ``preceding_tools`` count (how many
    ``postToolUse`` preceded it). We place a thought right before the
    ``(preceding_tools + 1)``-th transcript tool that actually fires
    ``postToolUse`` (``batch_tool_names``) — so it lands next to the real work it
    reasoned about, while transcript-only tools (Glob/UpdateCurrentStep) stay put.

    Attaches ``turn["thinking"] = [{content, position}]`` where ``position`` is
    the tool_calls index the thought should render before (``len(tool_calls)`` =
    end of turn). Applied to the first turn that has tool calls (Cursor subagents
    are effectively single-turn).
    """
    if not thinkings:
        return
    turns = conversation.get("turns") or []
    target = next(
        (t for t in turns if t.get("tool_calls")), turns[0] if turns else None
    )
    if target is None:
        return
    tool_calls = target.get("tool_calls") or []

    def _position_for(preceding: int) -> int:
        seen = 0
        for index, tool_call in enumerate(tool_calls):
            if tool_call.get("tool_name", "") in batch_tool_names:
                if seen == preceding:
                    return index
                seen += 1
        return len(tool_calls)

    target["thinking"] = [
        {
            "content": _cap_text(t["content"]),
            "position": _position_for(t["preceding_tools"]),
        }
        for t in thinkings
    ]


def _merge_tool_outputs(
    conversation: dict[str, Any], batch_tool_calls: list[dict[str, Any]]
) -> None:
    """Enrich a transcript conversation's tool calls with outputs, in place.

    The transcript is the ordered skeleton (every tool call, inputs only); the
    stray batch supplies the outputs. Matches per ``tool_name`` first-in-first-out
    so interleaved tool types line up without fragile input comparison.
    Transcript-only tools (e.g. Glob, UpdateCurrentStep, which fire no
    ``postToolUse``) keep ``tool_output = None``; surplus batch calls with no
    transcript match are dropped.
    """
    from collections import defaultdict, deque

    queues: dict[str, deque] = defaultdict(deque)
    for batch_call in batch_tool_calls:
        queues[batch_call.get("tool_name", "")].append(batch_call)

    for turn in conversation.get("turns") or []:
        for tool_call in turn.get("tool_calls") or []:
            queue = queues.get(tool_call.get("tool_name", ""))
            if not queue:
                continue
            batch_call = queue.popleft()
            if tool_call.get("tool_output") is None:
                tool_call["tool_output"] = batch_call.get("tool_output")
            if not tool_call.get("tool_call_id"):
                tool_call["tool_call_id"] = batch_call.get("tool_call_id", "")


def extract_subagent_conversation(
    parent_transcript_path: str,
    task: str,
    max_wait_s: float = 2.0,
    poll_s: float = 0.1,
    cleanup_child_batch: bool = True,
) -> dict[str, Any] | None:
    """Parse a Cursor subagent's transcript into a normalized conversation.

    Returns ``{"turns": [...]}`` (the backend's child-session shape) or None if
    no matching transcript is found. Cursor may fire ``subagentStop`` a moment
    before the child transcript's final assistant message is flushed, so this
    polls (bounded by ``max_wait_s``) until the file carries a ``turn_ended``
    marker before parsing.

    The transcript records tool INPUTS only and no thinking, so tool OUTPUTS and
    THINKING are merged in from the subagent's own stray hook batch
    (``_read_child_batch`` + ``_merge_tool_outputs`` + ``_attach_thinking``).
    Cursor exposes no subagent token usage anywhere, so token totals stay 0.

    Args:
        parent_transcript_path: ``payload.transcript_path``.
        task: ``payload.task`` — used to locate the child transcript.
        max_wait_s: Max seconds to wait for the transcript to flush.
        poll_s: Poll interval while waiting.
        cleanup_child_batch: Delete the subagent's stray hook batch after
            merging (default). The merged result is frozen into the parent
            batch envelope, so the orphan batch is no longer needed; removing it
            stops the batches dir from accumulating dead child files.

    Returns:
        The capped conversation dict, or None.
    """
    path = find_subagent_transcript(parent_transcript_path, task)
    if not path:
        return None

    from cursor_transcript import is_complete, parse_transcript

    deadline = time.monotonic() + max_wait_s
    while not is_complete(path) and time.monotonic() < deadline:
        time.sleep(poll_s)

    try:
        result = parse_transcript(path)
    except Exception:
        return None
    if isinstance(result, dict) and not result.get("turns"):
        # Empty/corrupt transcript parsed to zero turns — treat as absent so the
        # caller's `if conversation:` guard skips it instead of uploading an
        # empty subagent_transcript.
        return None
    if isinstance(result, dict):
        # Enrich the transcript (tool inputs only, no thinking) with the outputs
        # and thinking captured in the subagent's own stray hook batch, keyed by
        # the child conversation id (the transcript's filename stem).
        child_conv_id = os.path.splitext(os.path.basename(path))[0]
        tool_calls, thinkings = _read_child_batch(child_conv_id)
        _merge_tool_outputs(result, tool_calls)
        _attach_thinking(
            result, thinkings, {tc.get("tool_name", "") for tc in tool_calls}
        )
        _cap_conversation(result)
        if cleanup_child_batch:
            _delete_child_batch(child_conv_id)
    return result


def _delete_child_batch(child_conv_id: str) -> None:
    """Best-effort remove a subagent's orphaned stray hook batch file."""
    try:
        os.remove(get_batch_file(child_conv_id))
    except (OSError, ValueError):
        pass
