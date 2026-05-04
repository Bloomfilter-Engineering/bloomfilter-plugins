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

# Platform-specific stdlib modules used by ``_lock_file`` below.
if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.1.4"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_MAX_BYTES = 1_000_000  # 1 MB — rotation cap per file
DEBUG_LOG_BACKUP_COUNT = 1  # keep one rotated backup → ~2 MB max on disk

_debug_logger = None  # Lazy-init singleton; populated on first debug_log() call.


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

    Cursor / Claude / Codex inject a plugin data dir env var when present.
    Fall back to the bloomfilter config dir so the log lives next to the
    user's batches and config.json (~/.config/bloomfilter on macOS/Linux).
    """
    return (
        os.environ.get("PLUGIN_DATA")
        or os.environ.get("CURSOR_PLUGIN_DATA")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or get_config_dir()
    )


def _build_debug_logger():
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

    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=DEBUG_LOG_MAX_BYTES,
            backupCount=DEBUG_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03dZ %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime  # UTC timestamps
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def debug_log(message):
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


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def read_json_config(path, key, default=""):
    """Safely read a single key from a JSON config file."""
    try:
        with open(path, "r") as f:
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
        if platform.system() != "Windows":
            os.chmod(config_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        print(
            f"[bloomfilter] Created config at {config_file} — add your API key to get started.",
            file=sys.stderr,
        )

    return config_file


def resolve_api_key():
    """Resolve the API key: BLOOMFILTER_API_KEY env var > user config."""
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
    """Read JSON payload from stdin."""
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


def _resolve_git_executable():
    """Return a git executable path if available."""
    git = shutil.which("git")
    if git:
        return git

    if platform.system() != "Windows":
        return ""

    candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "Git", "cmd", "git.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Git", "bin", "git.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Git", "cmd", "git.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Git", "bin", "git.exe"),
        os.path.join(
            os.environ.get("LocalAppData", ""), "Programs", "Git", "cmd", "git.exe"
        ),
        os.path.join(
            os.environ.get("LocalAppData", ""), "Programs", "Git", "bin", "git.exe"
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    return ""


def get_git_branch(project_dir):
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
    def _lock_file(fp, exclusive=True):
        """Acquire an flock on an open file, release on exit."""
        op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fp, op)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)

else:

    @contextlib.contextmanager
    def _lock_file(fp, exclusive=True):
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
        except OSError:
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
        with _lock_file(f, exclusive=True):
            f.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def read_batch(session_id):
    """Read all entries from the batch file and return the list (no delete)."""
    batch_file = get_batch_file(session_id)
    if not os.path.isfile(batch_file):
        return []
    with open(batch_file, "r") as f:
        with _lock_file(f, exclusive=False):
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


def rewrite_batch(session_id, entries):
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


def clear_batch(session_id):
    """Truncate the batch file for *session_id* (race-safe).

    Delegates to ``rewrite_batch`` so the truncation is performed while
    holding the exclusive lock. Leaves a zero-byte file rather than
    deleting; ``read_batch`` returns ``[]`` for both cases.
    """
    rewrite_batch(session_id, [])


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


def upload_batch(api_url, api_key, payload):
    """POST raw hook batch to the Bloomfilter API. Returns True on success.

    Validates the URL scheme up front: only http/https are allowed. Other
    schemes (file://, ftp://, gopher://, ...) would otherwise be honoured
    by urllib.request.urlopen if a local config supplies a malicious url.

    Network interactions are logged to <plugin-data>/debug.log: the request
    URL + session_id + hook count, the response status + truncated body,
    and any HTTPError / URLError / unexpected exception.
    """
    parsed = urllib.parse.urlparse(api_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return False

    full_url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
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
        f"upload_batch: sending POST {full_url} session_id={session_id} "
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
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        reason = getattr(exc, "reason", "")
        debug_log(
            f"upload_batch: HTTPError status={exc.code} reason={reason!r} "
            f"session_id={session_id} body={body[:500]!r}"
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


def utcnow_iso():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
