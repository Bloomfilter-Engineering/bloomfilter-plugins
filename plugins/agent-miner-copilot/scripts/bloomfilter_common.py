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
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

# Platform-specific stdlib modules used by ``_lock_file`` below.
if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.3.0"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_TAG = "copilot"  # disambiguates plugins sharing the same log dir

# GitHub Copilot fires hooks in two payload conventions, selected by event-name
# casing: PascalCase event names (e.g. ``SubagentStop``) get snake_case fields,
# while camelCase event names get camelCase fields. ``hooks/hooks.json``
# registers PascalCase only — the CLI recognises those names and switches to
# snake_case payloads to match VS Code — so this table is defensive: it keeps
# older CLI builds (and any future camelCase-only event) reading uniformly.
#
# Registering both casings for the same event is NOT safe: VS Code maps e.g.
# ``Stop`` and ``agentStop`` to the same internal event and would fire the hook
# twice, double-capturing every turn.
#
# Subagent fields differ by runtime and are NOT interchangeable. VS Code sends
# ``agent_id`` / ``agent_type`` (already snake_case, same names Claude Code
# uses), where ``agent_id`` is the runSubagent ``tool_use_id`` minus its
# ``__vscode-<n>`` suffix. The Copilot CLI SDK instead declares ``agentName`` /
# ``agentDisplayName`` / ``agentDescription`` / ``stopReason`` — mapped below so
# a CLI subagent arrives intact, though none has been captured yet.
_CAMEL_TO_SNAKE_PAYLOAD_KEYS = {
    "sessionId": "session_id",
    "hookEventName": "hook_event_name",
    "transcriptPath": "transcript_path",
    "toolName": "tool_name",
    "toolArgs": "tool_input",
    "toolInput": "tool_input",
    "toolUseId": "tool_use_id",
    "toolResponse": "tool_response",
    "initialPrompt": "initial_prompt",
    "agentType": "agent_type",
    "agentId": "agent_id",
    "permissionMode": "permission_mode",
    "notificationType": "notification_type",
    # Copilot CLI subagent fields (SubagentStart/SubagentStop). VS Code sends
    # agent_id/agent_type instead — see the note above _CAMEL_TO_SNAKE_PAYLOAD_KEYS.
    "agentName": "agent_name",
    "agentDisplayName": "agent_display_name",
    "agentDescription": "agent_description",
    "stopReason": "stop_reason",
    "toolCallId": "tool_call_id",
}

# Cap on free-text subagent fields, matching the other agent-miner plugins.
_SUBAGENT_FIELD_CAP = 10_000

# Transcript read budget. Files up to MAX_TRANSCRIPT_BYTES are read whole so the
# delta format's base snapshot (line 0) survives; larger ones degrade to the
# last TAIL_WINDOW_BYTES so a runaway file can never be slurped on a hook.
MAX_TRANSCRIPT_BYTES = 10_000_000
TAIL_WINDOW_BYTES = 200_000


def _cap_text(value: str) -> str:
    """Truncate a string to the subagent field cap; return it unchanged otherwise."""
    if len(value) > _SUBAGENT_FIELD_CAP:
        return value[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
    return value


def normalize_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate camelCase hook payload keys to snake_case in-place.

    Non-destructive: a snake_case key already present is never overwritten;
    we only copy from a camelCase fallback when the snake_case form is
    missing. Returns *payload* for convenience.
    """
    if not isinstance(payload, dict):
        return payload
    for camel, snake in _CAMEL_TO_SNAKE_PAYLOAD_KEYS.items():
        if snake not in payload and camel in payload:
            payload[snake] = payload[camel]
    return payload


def detect_runtime(payload: dict[str, Any]) -> str:
    """Identify which Copilot runtime fired this hook.

    Returns ``"copilot-cli"`` or ``"copilot-vscode"``. The strongest signal
    is the ``transcript_path`` shape — the CLI writes ``events.jsonl`` under
    ``~/.copilot/session-state/<id>/`` while VS Code writes under
    ``workspaceStorage/<ws>/GitHub.copilot-chat/transcripts/`` or
    ``chatSessions/``. Falls back to environment variables.
    """
    transcript_path = ""
    if isinstance(payload, dict):
        transcript_path = payload.get("transcript_path", "") or ""
    if transcript_path:
        if (
            os.sep + ".copilot" + os.sep + "session-state" in transcript_path
            or "/.copilot/session-state/" in transcript_path
        ):
            return "copilot-cli"
        if (
            "GitHub.copilot-chat" in transcript_path
            or os.sep + "chatSessions" + os.sep in transcript_path
            or "/chatSessions/" in transcript_path
        ):
            return "copilot-vscode"
    # The CLI injects COPILOT_PLUGIN_ROOT/COPILOT_PLUGIN_DATA on every hook;
    # VS Code injects only CLAUDE_PLUGIN_ROOT. Check this before the VS Code
    # env vars so a CLI session running inside a VS Code integrated terminal
    # isn't misdetected as copilot-vscode — that would silently disable the
    # CLI dedup in collect_hook.py and double-count turns.
    if os.environ.get("COPILOT_PLUGIN_ROOT"):
        return "copilot-cli"
    if os.environ.get("VSCODE_PID") or os.environ.get("TERM_PROGRAM") == "vscode":
        return "copilot-vscode"
    if os.environ.get("COPILOT_HOME") or os.path.isdir(
        os.path.join(os.path.expanduser("~"), ".copilot", "session-state")
    ):
        return "copilot-cli"
    return "copilot-vscode"


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


def secure_makedirs(path: str) -> None:
    """Create directories with owner-only permissions on Unix."""
    os.makedirs(path, exist_ok=True)
    if platform.system() != "Windows":
        os.chmod(path, stat.S_IRWXU)  # 0o700


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------


def _resolve_debug_log_dir() -> str:
    """Return the directory for debug.log.

    Always the bloomfilter config dir (~/.config/bloomfilter on macOS/Linux,
    %APPDATA%\\bloomfilter on Windows).
    All agent-miner plugins write to the same well-known location so a single
    debug.log shows the full picture across Claude Code / Cursor / Codex /
    Copilot. The DEBUG_LOG_TAG prefix on each line disambiguates the source.
    """
    return get_config_dir()


def debug_log(message: str) -> None:
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


def read_json_config(path: str, key: str, default: str = "") -> str:
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
            f"[bloomfilter] Created config at {config_file} — add your API key to get started."
        )

    return config_file


def resolve_api_key() -> str:
    """Resolve the API key: env var > user config."""
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

    Uses utf-8-sig on Windows so a leading BOM is stripped — PowerShell
    pipes to a native executable can prefix stdout with a UTF-8 BOM on
    Windows PowerShell 5.1, which would otherwise break json.loads.
    """
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8-sig")
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


# ---------------------------------------------------------------------------
# Process spawning
# ---------------------------------------------------------------------------


def spawn_detached(args: list[str]) -> bool:
    """Launch *args* as a fully detached background process.

    Returns immediately. The child is decoupled from the parent's stdio and
    placed in its own session/process group, so it survives the parent (the
    hook) exiting and never blocks it. Returns True if the spawn succeeded.
    """
    try:
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if platform.system() == "Windows":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)
        return True
    except Exception:
        return False


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
    def _lock_file(fp: Any, exclusive: bool = True) -> Iterator[None]:
        """Acquire an flock on an open file, release on exit."""
        op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fp, op)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)

else:

    @contextlib.contextmanager
    def _lock_file(fp: Any, exclusive: bool = True) -> Iterator[None]:
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
        else:
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


def append_to_batch(session_id: str, entry: dict[str, Any]) -> None:
    """Append a single JSON line to the batch file for *session_id*."""
    batch_file = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(batch_file, "a") as f:
        with _lock_file(f, exclusive=True):
            f.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def read_batch(session_id: str) -> list[dict[str, Any]]:
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


def clear_batch(session_id: str) -> None:
    """Delete the batch file for *session_id*."""
    batch_file = get_batch_file(session_id)
    if os.path.isfile(batch_file):
        os.remove(batch_file)


def rewrite_batch(session_id: str, entries: list[dict[str, Any]]) -> None:
    """Re-write entries back to the batch file (used on upload failure)."""
    batch_file = get_batch_file(session_id)
    with open(batch_file, "w") as f:
        with _lock_file(f, exclusive=True):
            for entry in entries:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


def upload_batch(api_url: str, api_key: str, payload: dict[str, Any]) -> bool:
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


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Copilot transcript discovery and parsing
# ---------------------------------------------------------------------------


def _get_vscode_data_dirs() -> list[str]:
    """Return existing VS Code data directories for the current platform."""
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif system == "Windows":
        base = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    else:  # Linux
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    dirs = []
    for variant in ("Code", "Code - Insiders"):
        path = os.path.join(base, variant)
        if os.path.isdir(path):
            dirs.append(path)
    return dirs


# Cache: (session_id, chat_sessions_only) → transcript file path
# (avoid re-searching per hook)
_transcript_cache = {}


def derive_chat_sessions_path(transcript_path: str) -> str:
    """Derive the chatSessions path from a GitHub.copilot-chat/transcripts/ path.

    Both formats share the same workspace ID and UUID filename:
      old: .../workspaceStorage/{ws}/GitHub.copilot-chat/transcripts/{uuid}.jsonl
      new: .../workspaceStorage/{ws}/chatSessions/{uuid}.jsonl

    The new chatSessions format contains token counts and resolved model data
    that the old format lacks.

    Returns the chatSessions path if it exists on disk, or '' otherwise.
    """
    if not transcript_path:
        return ""

    marker = os.path.join("GitHub.copilot-chat", "transcripts")
    if marker not in transcript_path:
        return ""

    chat_sessions_path = transcript_path.replace(marker, "chatSessions")
    return chat_sessions_path if os.path.isfile(chat_sessions_path) else ""


def find_copilot_transcript(session_id: str, chat_sessions_only: bool = False) -> str:
    """Find the Copilot transcript file that contains the given session_id.

    Searches VS Code storage locations for JSONL transcript files and scans
    for the session_id in the file content.

    When *chat_sessions_only* is True, only the token/model-bearing
    ``chatSessions`` locations (workspace ``chatSessions/`` and global
    ``emptyWindowChatSessions/``) are searched; the old
    ``GitHub.copilot-chat/transcripts`` format — which carries messages but no
    tokens or model — is skipped. Use this when the caller needs token/model
    metadata, since parsing an old-format transcript yields 0 tokens and an
    empty model, which the re-upload worker treats as "not flushed yet".

    Args:
        session_id: The hook session_id to search for.
        chat_sessions_only: Restrict the search to chatSessions locations.

    Returns:
        str: Path to the transcript file, or '' if not found.
    """
    cache_key = (session_id, chat_sessions_only)
    if cache_key in _transcript_cache:
        return _transcript_cache[cache_key]

    if not session_id:
        return ""

    search_dirs = []
    for code_base in _get_vscode_data_dirs():
        # New format: globalStorage/emptyWindowChatSessions/
        global_dir = os.path.join(
            code_base, "User", "globalStorage", "emptyWindowChatSessions"
        )
        if os.path.isdir(global_dir):
            search_dirs.append(global_dir)

        # Workspace sessions: workspaceStorage/*/chatSessions/ (new format, has tokens)
        # and workspaceStorage/*/GitHub.copilot-chat/transcripts/ (old format, no tokens)
        ws_dir = os.path.join(code_base, "User", "workspaceStorage")
        if os.path.isdir(ws_dir):
            for ws in os.listdir(ws_dir):
                # Prefer chatSessions (new format with token data)
                chat_dir = os.path.join(ws_dir, ws, "chatSessions")
                if os.path.isdir(chat_dir):
                    search_dirs.append(chat_dir)
                # Fallback: old transcript format (no tokens/model) — skipped
                # when the caller only wants token/model-bearing files.
                if chat_sessions_only:
                    continue
                transcript_dir = os.path.join(
                    ws_dir, ws, "GitHub.copilot-chat", "transcripts"
                )
                if os.path.isdir(transcript_dir):
                    search_dirs.append(transcript_dir)

    if not search_dirs:
        return ""

    # Search most recently modified files first
    candidates = []
    for d in search_dirs:
        for fname in os.listdir(d):
            if fname.endswith(".jsonl"):
                fpath = os.path.join(d, fname)
                candidates.append((os.path.getmtime(fpath), fpath))

    candidates.sort(reverse=True)  # newest first

    for _, fpath in candidates:
        try:
            # Quick check: scan file for session_id string
            with open(fpath, "rb") as f:
                chunk = f.read(200_000)
            if session_id.encode() in chunk:
                _transcript_cache[cache_key] = fpath
                return fpath
        except Exception:
            continue

    return ""


def parse_copilot_transcript(transcript_path: str) -> dict[str, Any]:
    """Parse a Copilot transcript JSONL file.

    Handles both the new kind-based format (globalStorage) and the old
    type-based format (workspaceStorage).

    Returns a dict with:
      - requests: list[dict] — one record per request turn, each containing
            requestId, responseId, modelId, resolvedModel, userMessage,
            response_content, reasoning_text, reasoning_parts,
            input_tokens, output_tokens, timestamp.
      - response_content: str (latest agent response, for backward compat)
      - reasoning_text: str (latest thinking text, for backward compat)
      - input_tokens / output_tokens: int (latest turn, for backward compat)
      - result_count: int
      - model: str (latest turn, for backward compat)
      - subagents: dict[str, dict] — agent_id -> {model, credits, prompt,
            result, description} for every runSubagent call in the session.
            The only source of a subagent's model and cost; the hook stream
            carries neither.
    """
    empty = {
        "requests": [],
        "response_content": "",
        "reasoning_text": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "result_count": 0,
        "model": "",
        "subagents": {},
    }

    if not transcript_path or not os.path.exists(transcript_path):
        return empty

    try:
        # Read the whole file when it is within budget. The new format is a
        # delta log whose FIRST line is the base snapshot, so a tail-only read
        # discards it and everything reconstructed from it: measured on real
        # sessions, a 375 KB transcript parsed to 1 request with an empty model
        # (and no subagent records) instead of its full history. Line counts are
        # tiny — 20-60 lines even at 375 KB — so the cost is in bytes, not
        # parsing. Beyond the cap fall back to the bounded tail, which at least
        # keeps recent turns rather than reading an unbounded file on a hook.
        file_size = os.path.getsize(transcript_path)
        read_start = (
            0 if file_size <= MAX_TRANSCRIPT_BYTES else file_size - TAIL_WINDOW_BYTES
        )
        with open(transcript_path, "rb") as tf:
            if read_start > 0:
                tf.seek(read_start)
            # Cap the read itself: a file that grows after getsize() must not
            # let us slurp past the budget the read_start branch chose.
            raw = tf.read(TAIL_WINDOW_BYTES if read_start > 0 else MAX_TRANSCRIPT_BYTES)
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

        if not entries:
            return empty

        # Detect format and parse into per-request records
        first = entries[0]
        if "kind" in first:
            records = _parse_new_format(entries)
        elif first.get("type") == "session.start":
            records = _parse_old_format(entries)
        else:
            records = _parse_new_format(entries)
            if not records:
                records = _parse_old_format(entries)

        # Build result with backward-compat flat fields from last record
        result = {
            "requests": records,
            "response_content": "",
            "reasoning_text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "result_count": len(records),
            "model": "",
            # Flattened across every turn: agent_id -> subagent record. Keys are
            # globally unique tool-call ids, so turns can't collide.
            "subagents": {
                agent_id: data
                for rec in records
                for agent_id, data in (rec.get("subagents") or {}).items()
            },
        }
        for rec in reversed(records):
            if rec.get("response_content"):
                result["response_content"] = rec["response_content"]
                result["reasoning_text"] = rec.get("reasoning_text", "")
                result["input_tokens"] = rec.get("input_tokens", 0)
                result["output_tokens"] = rec.get("output_tokens", 0)
                result["model"] = rec.get("resolvedModel") or rec.get("modelId", "")
                break

        return result

    except Exception:
        return empty


def _set_nested(obj: Any, key_path: list[str | int], value: Any) -> None:
    """Set *value* at *key_path* inside a nested dict/list structure.

    Each segment in *key_path* is either a ``str`` (dict key) or ``int``
    (list index).  Missing intermediate containers are created automatically.
    """
    for i, segment in enumerate(key_path[:-1]):
        next_segment = key_path[i + 1]
        if isinstance(obj, dict):
            obj = obj.setdefault(segment, [] if isinstance(next_segment, int) else {})
        elif isinstance(obj, list) and isinstance(segment, int):
            while len(obj) <= segment:
                obj.append({})
            obj = obj[segment]
        else:
            return  # can't navigate further

    last = key_path[-1]
    if isinstance(obj, dict):
        obj[last] = value
    elif isinstance(obj, list) and isinstance(last, int):
        while len(obj) <= last:
            obj.append(None)
        obj[last] = value


def _reconstruct_session_state(entries: list[dict[str, Any]]) -> list[Any]:
    """Replay CRDT-style JSONL entries to materialise the ``requests`` array.

    * kind=0 — session init (seeds state)
    * kind=1 — patch at key path
    * kind=2 — array replace at key path

    Special handling for ``kind=2, k=["requests"]``: VS Code emits a
    compacted snapshot that drops completed requests, but subsequent
    ``kind=1`` patches still use absolute session indices.  We *merge*
    new requests instead of replacing to preserve earlier request data.

    Returns the fully materialised ``list[dict]`` of request objects.
    """
    state = {}
    for entry in entries:
        kind = entry.get("kind")
        if kind == 0:
            state = entry.get("v", {})
        elif kind in (1, 2):
            k = entry.get("k", [])
            v = entry.get("v")
            if not k:
                continue
            # kind=2 k=["requests"] — merge new requests, don't replace
            if kind == 2 and k == ["requests"] and isinstance(v, list):
                existing = state.setdefault("requests", [])
                existing_ids = {
                    r.get("requestId")
                    for r in existing
                    if isinstance(r, dict) and r.get("requestId")
                }
                for req in v:
                    if not isinstance(req, dict):
                        continue
                    rid = req.get("requestId", "")
                    if rid and rid in existing_ids:
                        # Update in place
                        for i, er in enumerate(existing):
                            if isinstance(er, dict) and er.get("requestId") == rid:
                                existing[i] = req
                                break
                    else:
                        existing.append(req)
            else:
                _set_nested(state, k, v)
    return state.get("requests", [])


def _extract_request_record(req: dict[str, Any]) -> dict[str, Any]:
    """Extract a structured record from a single materialised Copilot request.

    Returns a dict with per-request metadata, user message, response
    content, ordered reasoning parts, and token counts.
    """
    record = {
        "requestId": req.get("requestId", ""),
        "responseId": req.get("responseId", ""),
        "modelId": (req.get("modelId") or "").removeprefix("copilot/"),
        "resolvedModel": "",
        "userMessage": "",
        "response_content": "",
        "reasoning_text": "",
        "reasoning_parts": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "timestamp": req.get("timestamp", 0),
        # agent_id -> {model, credits, prompt, result, description} for any
        # runSubagent call made during this turn.
        "subagents": {},
    }

    # User message
    message = req.get("message")
    if isinstance(message, dict):
        record["userMessage"] = message.get("text", "")

    # --- Response parts (content + fallback reasoning) ---
    response_parts = req.get("response", [])
    content_parts = []
    fallback_reasoning = []
    if isinstance(response_parts, list):
        for part in response_parts:
            if not isinstance(part, dict):
                continue
            part_kind = part.get("kind", "")
            value = part.get("value", "")
            if not value:
                continue
            if part_kind == "thinking":
                fallback_reasoning.append(value)
            elif part_kind in ("", "markdownContent"):
                content_parts.append(value)

    if content_parts:
        record["response_content"] = "\n".join(content_parts)

    # --- Subagent invocations ---
    # A runSubagent call is serialized as a response part whose
    # ``toolSpecificData.kind`` is "subagent". It is the ONLY place VS Code
    # records the child's model and cost — the hook stream carries neither.
    # The part's ``toolCallId`` is the bare agent_id (the hook's ``agent_id``,
    # i.e. the PostToolUse ``tool_use_id`` without its ``__vscode-<n>`` suffix),
    # so it keys straight onto the envelope built at the runSubagent
    # PostToolUse.
    if isinstance(response_parts, list):
        for part in response_parts:
            if not isinstance(part, dict):
                continue
            data = part.get("toolSpecificData")
            if not isinstance(data, dict) or data.get("kind") != "subagent":
                continue
            agent_id = part.get("toolCallId", "")
            if not agent_id:
                continue
            record["subagents"][agent_id] = {
                "model": data.get("modelName", ""),
                "credits": data.get("credits"),
                "prompt": data.get("prompt", ""),
                "result": data.get("result", ""),
                "description": data.get("description", ""),
            }

    # --- Token counts, model, and ordered reasoning from result metadata ---
    result_obj = req.get("result")
    if isinstance(result_obj, dict):
        metadata = result_obj.get("metadata")
        if isinstance(metadata, dict):
            record["resolvedModel"] = metadata.get("resolvedModel", "")
            record["input_tokens"] = metadata.get("promptTokens", 0) or 0
            record["output_tokens"] = metadata.get("outputTokens", 0) or 0

            # Build ordered reasoning_parts from toolCallRounds — each round
            # represents one think→act cycle, so the order is preserved.
            tool_call_rounds = metadata.get("toolCallRounds")
            if isinstance(tool_call_rounds, list):
                for rnd in tool_call_rounds:
                    if not isinstance(rnd, dict):
                        continue
                    thinking = rnd.get("thinking")
                    if isinstance(thinking, dict) and thinking.get("text"):
                        record["reasoning_parts"].append(
                            {
                                "type": "thinking",
                                "content": thinking["text"],
                                "thinking_id": thinking.get("id", ""),
                                "timestamp": rnd.get("timestamp", 0),
                            }
                        )

    # If toolCallRounds wasn't available (result not flushed yet), fall back
    # to the inline response[] thinking blocks.
    if not record["reasoning_parts"] and fallback_reasoning:
        for text in fallback_reasoning:
            record["reasoning_parts"].append(
                {
                    "type": "thinking",
                    "content": text,
                    "thinking_id": "",
                    "timestamp": 0,
                }
            )

    # Flat reasoning_text for backward compat
    all_thinking = [p["content"] for p in record["reasoning_parts"]]
    if all_thinking:
        record["reasoning_text"] = "\n".join(all_thinking)

    return record


def _parse_new_format(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse the new kind-based transcript format.

    Replays CRDT entries to reconstruct the full session state, then
    extracts one record per request.

    Returns ``list[dict]`` of per-request records.
    """
    requests = _reconstruct_session_state(entries)
    return [_extract_request_record(r) for r in requests if isinstance(r, dict)]


def _parse_old_format(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse the old type-based transcript format (workspaceStorage).

    Returns ``list[dict]`` of per-request records (with empty IDs/model).
    """
    records = []
    for entry in entries:
        if entry.get("type") != "assistant.message":
            continue
        data = entry.get("data", {})
        content = data.get("content", "")
        reasoning = data.get("reasoningText", "")
        if not content and not reasoning:
            continue
        records.append(
            {
                "requestId": "",
                "responseId": "",
                "modelId": "",
                "resolvedModel": "",
                "userMessage": "",
                "response_content": content,
                "reasoning_text": reasoning,
                "reasoning_parts": (
                    [
                        {
                            "type": "thinking",
                            "content": reasoning,
                            "thinking_id": "",
                            "timestamp": 0,
                        }
                    ]
                    if reasoning
                    else []
                ),
                "input_tokens": 0,
                "output_tokens": 0,
                "timestamp": 0,
            }
        )
    return records


# ---------------------------------------------------------------------------
# Copilot CLI transcript parsing
# ---------------------------------------------------------------------------


def parse_cli_transcript(events_path: str) -> dict[str, Any]:
    """Parse a Copilot CLI ``events.jsonl`` into per-request records.

    The CLI's transcript_path on a hook points at
    ``~/.copilot/session-state/<id>/events.jsonl`` and uses a flat
    type-based event stream (``session.start``, ``session.model_change``,
    ``user.message``, ``assistant.turn_start``, ``assistant.message``,
    ``assistant.turn_end``). Tokens and model are written synchronously, so
    no flush wait is needed — but only ``outputTokens`` is exposed; input
    tokens are not in the CLI feed and will be estimated downstream.

    Returns the same dict shape as :func:`parse_copilot_transcript`.
    """
    empty = {
        "requests": [],
        "response_content": "",
        "reasoning_text": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "result_count": 0,
        "model": "",
    }

    if not events_path or not os.path.exists(events_path):
        return empty

    try:
        with open(events_path, "rb") as fh:
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return empty

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        return empty

    def new_record(user_text: str, model: str, ts: int) -> dict[str, Any]:
        return {
            "requestId": "",
            "responseId": "",
            "modelId": model,
            "resolvedModel": model,
            "userMessage": user_text or "",
            "response_content": "",
            "reasoning_text": "",
            "reasoning_parts": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "timestamp": ts or 0,
        }

    def is_empty(rec: dict[str, Any]) -> bool:
        return (
            not rec.get("response_content")
            and not rec.get("output_tokens")
            and not rec.get("responseId")
            and not rec.get("reasoning_text")
        )

    records = []
    current_model = ""
    pending_user = ""
    current = None  # in-progress turn record

    for entry in entries:
        evt_type = entry.get("type", "")
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        ts = entry.get("timestamp", 0)

        match evt_type:
            case "session.model_change":
                new_model = data.get("newModel")
                if new_model:
                    current_model = new_model

            case "user.message":
                # Drop a still-open turn that produced nothing (e.g. aborted retry).
                if current is not None:
                    if not is_empty(current):
                        records.append(current)
                    current = None
                pending_user = data.get("content", "") or ""

            case "assistant.turn_start":
                # Skip if we already have an open turn (turn_start firing twice
                # around an abort) — keep the existing record, ignore the dupe.
                if current is None:
                    current = new_record(pending_user, current_model, ts)
                    pending_user = ""

            case "assistant.message":
                if current is None:
                    # Some sessions emit assistant.message without an explicit
                    # turn_start; create the record opportunistically.
                    current = new_record(pending_user, current_model, ts)
                    pending_user = ""

                msg_model = data.get("model")
                if msg_model:
                    current["resolvedModel"] = msg_model
                    current["modelId"] = msg_model
                    current_model = msg_model

                tok = data.get("outputTokens", 0) or 0
                current["output_tokens"] += tok

                request_id = data.get("requestId")
                if request_id and not current["requestId"]:
                    current["requestId"] = request_id

                message_id = data.get("messageId") or data.get("serviceRequestId")
                if message_id:
                    current["responseId"] = message_id

                content = data.get("content")
                if content:
                    # Take the latest non-empty content as the turn response.
                    current["response_content"] = content

            case "assistant.reasoning":
                # CLI emits a separate reasoning event with the thinking text.
                content = data.get("content")
                if content and current is not None:
                    current["reasoning_parts"].append(
                        {
                            "type": "thinking",
                            "content": content,
                            "thinking_id": data.get("reasoningId", ""),
                            "timestamp": ts,
                        }
                    )
                    if current["reasoning_text"]:
                        current["reasoning_text"] += "\n" + content
                    else:
                        current["reasoning_text"] = content

            case "abort":
                # User-cancelled or system-aborted turn: drop if empty.
                if current is not None and is_empty(current):
                    current = None

            case "assistant.turn_end":
                if current is not None:
                    if not is_empty(current):
                        records.append(current)
                    current = None

    if current is not None and not is_empty(current):
        records.append(current)

    result = {
        "requests": records,
        "response_content": "",
        "reasoning_text": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "result_count": len(records),
        "model": "",
    }
    for rec in reversed(records):
        if rec.get("response_content") or rec.get("output_tokens"):
            result["response_content"] = rec.get("response_content", "")
            result["output_tokens"] = rec.get("output_tokens", 0)
            result["model"] = rec.get("resolvedModel") or rec.get("modelId", "")
            break
    return result
