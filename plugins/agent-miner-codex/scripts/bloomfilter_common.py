from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator, TextIO

if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION: str = "0.1.0"
DEFAULT_API_URL: str = "https://api.bloomfilter.app"
DEBUG_LOG_NAME: str = "debug.log"


def _resolve_debug_log_dir() -> str:
    """Return the directory for debug.log.

    Codex injects ${PLUGIN_DATA} / ${CLAUDE_PLUGIN_DATA} into hook env, pointing
    at ~/.codex/plugins/data/<plugin>-<marketplace>/. Use that when present so
    diagnostic logs live alongside other plugin data per Codex convention.
    Fall back to the bloomfilter config dir for non-hook invocations
    (manual tests, future tools).
    """
    return (
        os.environ.get("PLUGIN_DATA")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or get_config_dir()
    )


def debug_log(message: str) -> None:
    """Append a timestamped line to <plugin-data>/debug.log.

    Always writes — silent on failure. Intended for ops/diagnostic visibility
    of upload events without polluting Codex's TUI stderr.
    """
    try:
        log_dir = _resolve_debug_log_dir()
        secure_makedirs(log_dir)
        log_path = os.path.join(log_dir, DEBUG_LOG_NAME)
        line = f"{datetime.now(timezone.utc).isoformat()} {message}\n"
        with open(log_path, "a") as log_file:
            log_file.write(line)
        if platform.system() != "Windows":
            os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        # Logger must never crash the hook.
        pass


def get_config_dir() -> str:
    """Return the Bloomfilter config directory for the current platform."""
    system_name = platform.system()
    if system_name == "Windows":
        appdata_dir = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(appdata_dir, "bloomfilter")
    xdg_config_home = os.environ.get(
        "XDG_CONFIG_HOME",
        os.path.join(os.path.expanduser("~"), ".config"),
    )
    return os.path.join(xdg_config_home, "bloomfilter")


def secure_makedirs(directory_path: str) -> None:
    """Create directories with owner-only permissions on Unix."""
    os.makedirs(directory_path, exist_ok=True)
    if platform.system() != "Windows":
        os.chmod(directory_path, stat.S_IRWXU)  # 0o700


def read_json_config(config_path: str, key: str, default: str = "") -> str:
    """Safely read a single key from a JSON config file."""
    try:
        with open(config_path, "r") as config_file:
            return json.load(config_file).get(key, default) or default
    except Exception:
        return default


def bootstrap_config(plugin_root: str) -> str:
    """Create the user config from the plugin template if it does not exist.

    Returns the absolute path to the user config file.
    """
    config_dir = get_config_dir()
    config_file_path = os.path.join(config_dir, "config.json")
    template_path = os.path.join(plugin_root, "bloomfilter.config.json")

    if not os.path.isfile(config_file_path):
        secure_makedirs(config_dir)
        if os.path.isfile(template_path):
            shutil.copy2(template_path, config_file_path)
        else:
            with open(config_file_path, "w") as config_file:
                json.dump({"api_key": "", "url": ""}, config_file, indent=2)
                config_file.write("\n")
        if platform.system() != "Windows":
            os.chmod(config_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    return config_file_path


def resolve_api_key() -> str:
    """Resolve the API key from env var or user config only."""
    api_key_from_env = os.environ.get("BLOOMFILTER_API_KEY", "")
    if api_key_from_env:
        return api_key_from_env

    user_config_path = os.path.join(get_config_dir(), "config.json")
    return read_json_config(user_config_path, "api_key")


def resolve_api_url(project_dir: str) -> str:
    """Resolve the API URL: env var > project config > user config > default."""
    api_url_from_env = os.environ.get("BLOOMFILTER_URL", "")
    if api_url_from_env:
        return api_url_from_env

    project_config_path = os.path.join(project_dir, ".bloomfilter", "config.json")
    user_config_path = os.path.join(get_config_dir(), "config.json")

    if os.path.isfile(project_config_path):
        project_url = read_json_config(project_config_path, "url")
        if project_url:
            return project_url

    user_url = read_json_config(user_config_path, "url")
    if user_url:
        return user_url

    return DEFAULT_API_URL


def read_payload() -> dict[str, Any]:
    """Read a JSON hook payload from stdin."""
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8")
    raw_payload = sys.stdin.read()
    return json.loads(raw_payload) if raw_payload.strip() else {}


def get_git_branch(project_dir: str) -> str:
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


if platform.system() != "Windows":

    @contextlib.contextmanager
    def _lock_file(file_handle: TextIO, exclusive: bool = True) -> Iterator[None]:
        """Acquire an flock on an open file, release on exit."""
        lock_operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(file_handle, lock_operation)
        try:
            yield
        finally:
            fcntl.flock(file_handle, fcntl.LOCK_UN)

else:

    @contextlib.contextmanager
    def _lock_file(file_handle: TextIO, exclusive: bool = True) -> Iterator[None]:
        """Cross-process byte-range lock on Windows via msvcrt.locking."""
        try:
            file_handle.flush()
        except (OSError, ValueError):
            pass
        try:
            seek_position: int | None = file_handle.tell()
        except (OSError, ValueError):
            seek_position = None

        try:
            file_handle.seek(0)
            msvcrt.locking(file_handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            # LK_LOCK is blocking-with-retries; reaching here means we genuinely
            # failed to acquire the lock. Don't yield — callers must not proceed
            # to write without a lock or they may corrupt the JSONL batch when
            # concurrent hook subprocesses race. Restore position and re-raise;
            # collect_hook.main() swallows exceptions at the top so the hook
            # silently no-ops instead of writing unsafely.
            if seek_position is not None:
                try:
                    file_handle.seek(seek_position)
                except (OSError, ValueError):
                    pass
            raise

        try:
            if seek_position is not None:
                file_handle.seek(seek_position)
            yield
        finally:
            try:
                file_handle.seek(0)
                msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            if seek_position is not None:
                try:
                    file_handle.seek(seek_position)
                except (OSError, ValueError):
                    pass


def get_batch_dir() -> str:
    """Return and create the Bloomfilter hook batch directory."""
    batch_dir = os.path.join(get_config_dir(), "batches")
    secure_makedirs(batch_dir)
    return batch_dir


def get_batch_file(session_id: str) -> str:
    """Return path to the JSONL batch file for session_id."""
    safe_session_id = os.path.basename(session_id)
    if not safe_session_id or safe_session_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_session_id}.jsonl")


def append_to_batch(session_id: str, entry: dict[str, Any]) -> None:
    """Append one JSON object to the session batch file."""
    batch_file_path = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(batch_file_path, "a") as batch_file:
        with _lock_file(batch_file, exclusive=True):
            batch_file.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def read_batch(session_id: str) -> list[dict[str, Any]]:
    """Read all valid JSON entries from a session batch file."""
    batch_file_path = get_batch_file(session_id)
    if not os.path.isfile(batch_file_path):
        return []
    with open(batch_file_path, "r") as batch_file:
        with _lock_file(batch_file, exclusive=False):
            raw_lines = batch_file.readlines()
    entries: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        try:
            entries.append(json.loads(stripped_line))
        except json.JSONDecodeError:
            continue
    return entries


def rewrite_batch(session_id: str, entries: list[dict[str, Any]]) -> None:
    """Rewrite a session batch while holding an exclusive lock."""
    batch_file_path = get_batch_file(session_id)
    with open(batch_file_path, "a+") as batch_file:
        with _lock_file(batch_file, exclusive=True):
            batch_file.seek(0)
            batch_file.truncate()
            for entry in entries:
                batch_file.write(json.dumps(entry, separators=(",", ":")) + "\n")
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def clear_batch(session_id: str) -> None:
    """Clear a session batch without deleting the coordination file."""
    rewrite_batch(session_id, [])


def freeze_batch(session_id: str) -> list[dict[str, Any]]:
    """Atomically read and truncate the batch under an exclusive lock.

    Returns the entries that were in the file at the moment of the call.
    The original file is left empty (but not deleted) so concurrent appenders
    keep working. Use this on Stop instead of read_batch to avoid re-uploading
    previous turns' events on every subsequent Stop.

    On upload failure, callers should re-append the entries with append_to_batch
    so they get retried on the next Stop.
    """
    batch_file_path = get_batch_file(session_id)
    if not os.path.isfile(batch_file_path):
        return []
    entries: list[dict[str, Any]] = []
    with open(batch_file_path, "a+") as batch_file:
        with _lock_file(batch_file, exclusive=True):
            batch_file.seek(0)
            raw_lines = batch_file.readlines()
            batch_file.seek(0)
            batch_file.truncate()
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    for raw_line in raw_lines:
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        try:
            entries.append(json.loads(stripped_line))
        except json.JSONDecodeError:
            continue
    return entries


def upload_batch(api_url: str, api_key: str, payload: dict[str, Any]) -> bool:
    """POST a raw hook batch to the Bloomfilter API.

    Logs request URL, response status, and (truncated) body to the debug log.
    Returns True on 2xx, False otherwise.
    """
    parsed_url = urllib.parse.urlparse(api_url or "")
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        return False

    full_url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    hook_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0
    debug_log(
        f"upload_batch: POST {full_url} session_id={session_id} hooks={hook_count}"
    )

    try:
        request_body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            full_url,
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "X-MCP-Token": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            status_code = response.status
            response_body = response.read().decode("utf-8", errors="replace")
        debug_log(
            f"upload_batch: response status={status_code} body={response_body[:500]!r}"
        )
        return 200 <= status_code < 300
    except urllib.error.HTTPError as http_error:
        try:
            error_body = http_error.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""
        debug_log(
            f"upload_batch: HTTPError status={http_error.code} body={error_body[:500]!r}"
        )
        return False
    except Exception as error:
        debug_log(f"upload_batch: error={type(error).__name__}: {error}")
        return False


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
