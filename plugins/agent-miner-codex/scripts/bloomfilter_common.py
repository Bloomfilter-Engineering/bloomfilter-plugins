from __future__ import annotations

import contextlib
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
from typing import Any, Iterator, TextIO

if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION: str = "0.2.0"
_SUBAGENT_FIELD_CAP: int = 10_000
DEFAULT_API_URL: str = "https://api.bloomfilter.app"
DEBUG_LOG_NAME: str = "debug.log"
DEBUG_LOG_TAG: str = "codex"  # disambiguates plugins sharing the same log dir


def _resolve_debug_log_dir() -> str:
    """Return the directory for debug.log.

    Always the bloomfilter config dir (%APPDATA%\\bloomfilter on Windows,
    $XDG_CONFIG_HOME/bloomfilter elsewhere). Codex injects PLUGIN_DATA /
    CLAUDE_PLUGIN_DATA pointing at ~/.codex/plugins/data/<plugin>-<mp>/, but
    we deliberately ignore those so debug.log lives next to the user's
    config.json and batches/ — one well-known place to look for diagnostics.
    """
    return get_config_dir()


def debug_log(message: str) -> None:
    """Append a timestamped line to <plugin-data>/debug.log.

    Always writes — silent on failure. Intended for ops/diagnostic visibility
    of upload events without polluting Codex's TUI stderr.
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
    """Safely read a single key from a JSON config file.

    Opens with utf-8-sig so a leading BOM is stripped — `Set-Content -Encoding
    UTF8` on Windows PowerShell 5.1 writes a BOM, and the README's Windows setup
    snippet uses exactly that, so user-created configs land here BOM-prefixed.
    """
    try:
        with open(config_path, "r", encoding="utf-8-sig") as config_file:
            value = json.load(config_file).get(key, default)
            return value if isinstance(value, str) and value else default
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


def resolve_api_url() -> str:
    """Resolve the API URL: env var > user config > default.

    Project-scoped overrides via ./.bloomfilter/config.json were removed
    intentionally — a checked-in project config could redirect uploads to
    an attacker-controlled host. URL is user-controlled only.
    """
    api_url_from_env = os.environ.get("BLOOMFILTER_URL", "")
    if api_url_from_env:
        return api_url_from_env

    user_config_path = os.path.join(get_config_dir(), "config.json")
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


def upload_batch(api_url: str, api_key: str, payload: dict[str, Any]) -> bool:
    """POST a raw hook batch to the Bloomfilter API. Returns True on 2xx.

    Validates the URL scheme up front: only http/https are allowed.

    Network interactions are logged to <plugin-data>/debug.log: the request
    URL + session_id + hook count + payload bytes, the response status +
    truncated body, and any HTTPError / URLError / unexpected exception.
    """
    parsed_url = urllib.parse.urlparse(api_url or "")
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
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
        request_body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} error={type(exc).__name__}: {exc}"
        )
        return False

    debug_log(
        f"upload_batch: sending POST {full_url} session_id={session_id} "
        f"hooks={hook_count} bytes={len(request_body)}"
    )

    try:
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
            f"upload_batch: response status={status_code} session_id={session_id} "
            f"body={response_body[:500]!r}"
        )
        if status_code != 201:
            print(
                f"[bloomfilter] Upload response status: {status_code}", file=sys.stderr
            )
        return 200 <= status_code < 300
    except urllib.error.HTTPError as http_error:
        try:
            error_body = http_error.read().decode("utf-8", errors="replace").strip()
        except Exception:
            error_body = ""
        reason = getattr(http_error, "reason", "")
        debug_log(
            f"upload_batch: HTTPError status={http_error.code} reason={reason!r} "
            f"session_id={session_id} body={error_body[:500]!r}"
        )
        message = f"[bloomfilter] Upload failed with HTTP {http_error.code}"
        if reason:
            message += f" {reason}"
        print(message, file=sys.stderr)
        if error_body:
            print(
                f"[bloomfilter] Upload response body: {error_body[:500]}",
                file=sys.stderr,
            )
        return False
    except urllib.error.URLError as url_error:
        debug_log(
            f"upload_batch: URLError session_id={session_id} reason={url_error.reason!r}"
        )
        print(f"[bloomfilter] Upload failed: {url_error.reason}", file=sys.stderr)
        return False
    except Exception as error:
        debug_log(
            f"upload_batch: error session_id={session_id} "
            f"type={type(error).__name__} message={error!s}"
        )
        print(f"[bloomfilter] Upload failed: {error}", file=sys.stderr)
        return False


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _cap_text(value: Any) -> Any:
    """Truncate an over-long string field; pass non-strings through unchanged.

    Subagent transcripts can carry very large tool outputs / responses. Cap
    them so a single batch upload stays bounded, mirroring the Claude Code
    plugin's behavior and the backend's field expectations.
    """
    if not isinstance(value, str):
        return value
    if len(value) > _SUBAGENT_FIELD_CAP:
        return value[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
    return value


def _cap_conversation(conversation: dict[str, Any]) -> None:
    """Cap the free-text fields of a parsed subagent conversation in place.

    Caps ``user_prompt``, ``agent_response``, and each tool call's
    ``tool_output``. ``tool_input`` is left raw (matches the main-session
    ToolCall shape and the Claude Code plugin).
    """
    for turn in conversation.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        if turn.get("user_prompt") is not None:
            turn["user_prompt"] = _cap_text(turn["user_prompt"])
        if turn.get("agent_response") is not None:
            turn["agent_response"] = _cap_text(turn["agent_response"])
        for tool_call in turn.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("tool_output") is not None:
                tool_call["tool_output"] = _cap_text(tool_call["tool_output"])


def extract_subagent_conversation(
    agent_transcript_path: str,
    expected_last_message: str | None = None,
    max_wait_s: float = 2.0,
    poll_s: float = 0.1,
) -> dict[str, Any] | None:
    """Parse a subagent's own Codex rollout into a normalized conversation.

    Returns ``{"turns": [...]}`` (the backend's child-session shape) or None if
    the transcript path is missing. ``agent_transcript_path`` points at the
    subagent thread's rollout JSONL.

    Codex fires ``SubagentStop`` before the subagent's final assistant message
    is guaranteed flushed to its rollout, so — when ``expected_last_message``
    (the authoritative ``payload.last_assistant_message``) is provided — this
    re-parses until the last turn's ``agent_response`` matches it, up to
    ``max_wait_s``. On timeout it backfills the final response from the
    authoritative message so a partial capture can't survive.
    """
    if not agent_transcript_path or not os.path.exists(agent_transcript_path):
        return None

    # Local import: codex_rollout lives beside this module on sys.path (the
    # hook entrypoint inserts the scripts dir before importing).
    from codex_rollout import parse_transcript

    expected = (expected_last_message or "").strip()
    expected_capped = (_cap_text(expected) or "").strip()
    deadline = time.monotonic() + max_wait_s
    result: dict[str, Any] | None = None
    matched = False
    while True:
        try:
            result = parse_transcript(agent_transcript_path)
        except Exception:
            result = None
        if isinstance(result, dict):
            _cap_conversation(result)
        if not expected:
            break
        last_response = ""
        if result and result.get("turns"):
            last_response = (result["turns"][-1].get("agent_response") or "").strip()
        matched = bool(last_response) and last_response == expected_capped
        if matched or time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    # Never confirmed a complete match → the final response is missing or was
    # partially flushed. Replace it with the authoritative message.
    if result and expected and not matched and result.get("turns"):
        result["turns"][-1]["agent_response"] = _cap_text(expected_last_message)
    return result
