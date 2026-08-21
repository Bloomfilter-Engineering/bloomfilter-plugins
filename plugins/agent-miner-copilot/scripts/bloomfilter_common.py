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
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Optional, IO, Any

# Platform-specific stdlib modules used by ``_lock_file`` below.
if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.4.0"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_TAG = "copilot"  # disambiguates plugins sharing the same log dir

# Socket timeout for the batch upload, in seconds. Deliberately under the
# per-hook timeout the runtime enforces for the hooks that upload (see
# hooks/hooks.json), so that a stalled POST raises URLError *inside* this
# process — which the debug log records and which leaves the batch intact for
# the next attempt — instead of the runtime killing the process mid-request.
# When the two budgets are equal the runtime wins the race and every stall
# becomes an unlogged kill, so re-check this against the shortest uploading
# hook whenever those timeouts change.
UPLOAD_TIMEOUT_S = 15

# Wall-clock ceiling for the 413 halving retries in one hook. Each POST can burn
# the full upload timeout, so an unbounded retry loop would overrun the runtime's
# hook timeout and be SIGKILLed mid-request — the failure that timeout budget
# exists to avoid. Exceeding it simply defers the rest to the next hook.
UPLOAD_RETRY_BUDGET_S = 8


# Largest request body this client will attempt, in bytes. The collector API
# rejects anything above its own ceiling, which this client cannot raise, so
# staying below it is the client's responsibility. The margin left underneath
# absorbs JSON escaping and the envelope that wraps the entries measured here.
MAX_UPLOAD_BYTES = 2_000_000

# Whether this runtime re-sends the whole batch on every upload instead of
# removing what it has already delivered. It decides two things that must stay
# consistent: how large a batch may be retained, and what to do when the
# envelopes that can never be shed already exceed one request.
BATCH_IS_CUMULATIVE = True

# Size beyond which a batch file sheds its low-value envelopes. Chunked upload
# alone does not bound the file: a session that appends faster than it drains
# still grows without limit.
#
# A runtime that removes what it has delivered gets several uploads' worth of
# headroom, so a healthy batch is never evicted.
#
# A runtime that re-sends the whole batch gets less than one: for it the retained
# file IS the request body, so it has to stay inside the same ceiling a single
# request does or nothing in it can ever be delivered again. It stops short of
# that ceiling deliberately. A turn is only shed once its closing envelope has
# been delivered, and that envelope is appended after the turn's tool traffic —
# so a turn allowed to fill the request budget on tool records alone would push
# its own terminator past the cut, never get it delivered, and never become
# shed-able. The remaining quarter is the room that terminator needs.
MAX_BATCH_RETAINED_BYTES = (
    (MAX_UPLOAD_BYTES * 3) // 4 if BATCH_IS_CUMULATIVE else 8 * MAX_UPLOAD_BYTES
)

# Low-water mark eviction drops to. Evicting to exactly MAX_BATCH_RETAINED_BYTES
# would park the file on the threshold, so the very next append crosses it again
# and pays a full read-parse-rewrite on the very next append, which is orders of
# magnitude dearer than the plain append it replaces. Dropping well below buys
# many cheap appends before the next eviction, keeping this off the hot path that
# per-tool hooks run thousands of times per session.
BATCH_EVICTION_TARGET_BYTES = MAX_BATCH_RETAINED_BYTES // 2

# Age at which an undrained batch file is collected. A batch is only removed
# once it has been fully uploaded, so a session that dies mid-flight leaves its
# file behind forever; without an expiry the directory grows without bound.
BATCH_MAX_AGE_SECONDS = 14 * 24 * 60 * 60

# Outcomes of a batch upload. A 413 has to be distinguishable from every other
# failure: the caller responds to it by sending less, whereas retrying an
# identical oversize body can never succeed.
UPLOAD_OK = "ok"
UPLOAD_FAILED = "failed"
UPLOAD_TOO_LARGE = "too-large"

# Envelopes that open a turn. Never shed on size, however large they get: on a
# runtime that supplies no per-turn key, these are the only thing that marks
# where one turn ends and the next begins, so one going missing shifts every
# turn after it.
TURN_START_HOOK_EVENTS = frozenset({"UserPromptSubmit"})

# Keys that identify the runtime a payload came from. Agent runtimes discover and
# execute each other's collectors -- one editor runs another's hook scripts from
# its plugin cache -- so a collector can be handed a session it does not serve.
# It has no way to tell from the event name alone, because the names it is given
# are its own. These are payload keys observed in every envelope from another
# runtime and in none from this one, so their presence identifies the sender.
FOREIGN_RUNTIME_MARKERS = frozenset({"cursor_version"})

# Envelopes carrying turn identity and token counts. They are a small fraction of
# envelope count, while tool-output envelopes dominate the bytes, so shedding by
# size alone would discard precisely the records the upload exists to deliver.
# Never evicted.
PROTECTED_HOOK_EVENTS = frozenset({"UserPromptSubmit", "Stop", "SessionStart"})

# Evicted only after every unprotected envelope is gone: the subagent-stop
# envelope carries a child session's token totals, but can grow to a large share
# of a batch, so it cannot be protected outright.
DEFERRED_HOOK_EVENTS = frozenset({"SubagentStop"})

# Envelopes that close a turn. A request is cut here where possible so a turn is
# never split across two of them: a turn's records belong together, and sending
# them whole is what keeps each request self-describing rather than dependent on
# one that came before it.
TURN_TERMINAL_HOOK_EVENTS = frozenset({"Stop"})


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


def _cap_text(value: Any) -> Any:
    """Truncate a string to the subagent field cap; return it unchanged otherwise."""
    # Anything that is not text is returned as it came: a transcript block
    # whose field is a container must not raise out of extraction.
    if not isinstance(value, str):
        return value
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
        # A variable that is set but empty must fall back, not resolve to "".
        # os.environ.get returns the default only when the key is absent, so an
        # empty value would make this path relative and land the batch — prompts
        # and reasoning text in cleartext — in the hook's working directory,
        # which is the user's project.
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        if not os.path.isabs(base):
            base = os.path.expanduser("~")
        return os.path.join(base, "bloomfilter")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    if not os.path.isabs(xdg):
        xdg = os.path.join(os.path.expanduser("~"), ".config")
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


def _sanitize_api_key(raw_key: str) -> str:
    """Return a key safe to place in a header, or '' when it is not.

    Surrounding whitespace is common when a key is copied, so it is trimmed.
    A key still containing a control character is rejected outright rather than
    cleaned: the HTTP client raises on such a value and its exception message
    is the header value itself, which would put the key verbatim into the debug
    log and the user's terminal. Refusing early keeps it out of both.

    Args:
        raw_key: Key as read from the environment or the config file.

    Returns:
        The trimmed key, or '' when it cannot be used safely.
    """
    key = (raw_key or "").strip()
    if not key:
        return ""
    # isascii as well as isprintable: the HTTP client encodes header values as
    # latin-1 and raises on anything outside it, and its exception text can carry
    # part of the value into a log.
    if (
        any(character in key for character in "\r\n\x00")
        or not key.isprintable()
        or not key.isascii()
    ):
        # Never log or echo the value -- only that one was rejected, and why.
        debug_log(
            "resolve_api_key: rejected a key containing control characters "
            f"(length={len(key)})"
        )
        return ""
    return key


def resolve_api_key() -> str:
    """Resolve the API key: env var > user config."""
    key = os.environ.get("BLOOMFILTER_API_KEY", "")
    if key:
        return _sanitize_api_key(key)

    user_config = os.path.join(get_config_dir(), "config.json")
    return _sanitize_api_key(read_json_config(user_config, "api_key"))


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
    # shutil.which can return a cwd-relative hit, and a process started
    # without an explicit executable path searches the current directory
    # before PATH on some platforms -- so an opened folder shipping its own
    # git-named binary would run instead of the real one. An absolute path
    # to a real file is the only form that cannot be redirected that way.
    if git and os.path.isabs(git) and os.path.isfile(git):
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
    # Refuse a symlinked batch directory. The config root is taken from the
    # environment, so anything able to set that for the editor's child processes
    # could point this at a directory of its choosing -- and the age sweep below
    # deletes files there. A real directory is the only thing safe to sweep.
    if os.path.islink(batch_dir):
        debug_log(f"get_batch_dir: refusing symlinked batch dir {batch_dir}")
        raise RuntimeError("batch directory must not be a symlink")
    secure_makedirs(batch_dir)
    return batch_dir


def get_batch_file(session_id: str) -> str:
    """Return path to the JSONL batch file for *session_id*."""
    safe_id = os.path.basename(session_id)
    if not safe_id or safe_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_id}.jsonl")


def _delivered_marker_path(session_id: str) -> str:
    """Return the path of the sidecar recording how much of a batch was sent.

    Args:
        session_id: Session the batch belongs to.

    Returns:
        Absolute path to the marker file.
    """
    return get_batch_file(session_id) + ".sent"


def read_delivered_prefix(session_id: str) -> int:
    """Return how many leading records of a batch have been delivered.

    Only meaningful on a runtime that re-sends the whole batch: there every
    upload starts at the first record, so a single count describes exactly which
    records the collector has already seen.

    Args:
        session_id: Session to read the marker for.

    Returns:
        The recorded count, or 0 when there is no usable marker.
    """
    try:
        with open(_delivered_marker_path(session_id)) as marker_file:
            return max(0, int(marker_file.read().strip() or 0))
    except (OSError, ValueError):
        return 0


def record_delivered_prefix(session_id: str, record_count: int) -> None:
    """Record that the first *record_count* records of a batch were delivered.

    Monotonic: a smaller count never lowers the mark, because a later, smaller
    request does not un-deliver what an earlier one already sent. Failure to
    write is not an error — the mark is an optimisation, and a missing one only
    makes the size guard more conservative.

    Args:
        session_id: Session that was uploaded.
        record_count: How many leading records the successful request carried.

    Returns:
        None.
    """
    if record_count <= 0:
        return
    highest = max(read_delivered_prefix(session_id), record_count)
    marker_path = _delivered_marker_path(session_id)
    try:
        with open(marker_path, "w") as marker_file:
            marker_file.write(str(highest))
        if platform.system() != "Windows":
            os.chmod(marker_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        return


def _reduce_delivered_prefix(session_id: str, removed_record_count: int) -> None:
    """Lower a session's delivered count after records leave the head of its file.

    The count is a position, not a set of record ids, so it only means anything
    relative to the current file. Draining without lowering it leaves a count
    larger than the file holds, and eviction reads that count to decide which
    records have already been delivered safely -- so a session that appends again
    has its fresh, unsent records treated as already sent.

    Args:
        session_id: Session whose marker is lowered.
        removed_record_count: How many records were removed from the head.

    Returns:
        None.
    """
    if removed_record_count <= 0:
        return
    remaining = max(read_delivered_prefix(session_id) - removed_record_count, 0)
    marker_path = _delivered_marker_path(session_id)
    try:
        with open(marker_path, "w") as marker_file:
            marker_file.write(str(remaining))
        if platform.system() != "Windows":
            os.chmod(marker_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        return


def _safe_eviction_limit(entries: list[dict[str, Any]], delivered_count: int) -> int:
    """Return how far into a batch it is safe to evict.

    Applies to runtimes that re-send the whole batch. Removing a record that has
    already been sent retracts it, because the collector rebuilds an unfinished
    turn from whatever the newest upload carries. A turn whose closing envelope
    has already been delivered is finished, though, and a finished turn is not
    rebuilt — so everything up to and including the last delivered turn
    terminator can be shed safely, and nothing after it can.

    Args:
        entries: The batch snapshot, in file order.
        delivered_count: Leading records known to have been delivered.

    Returns:
        The exclusive upper index eviction may touch. Zero means evict nothing.
    """
    limit = min(delivered_count, len(entries))
    for index in range(limit - 1, -1, -1):
        entry = entries[index]
        if not isinstance(entry, dict):
            continue
        if entry.get("hook_event_name", "") in TURN_TERMINAL_HOOK_EVENTS:
            return index + 1
    return 0


def _append_is_refused(session_id: str, entry: dict[str, Any]) -> bool:
    """Return True when a low-value record must not be added to a full batch.

    Only ever refuses on a runtime that re-sends the whole batch: for those the
    retained file IS the request body, and the safe way to bound it is to stop
    taking new low-value records rather than to remove ones already sent. Taking
    a record back out after it has been delivered retracts it, because the
    collector rebuilds an unfinished turn from whatever the newest upload
    carries; refusing a record that was never sent costs only that record.

    Records that carry turn identity or token counts are always accepted, so the
    turn skeleton survives whatever the batch size. Refusal is logged once per
    append so a session that hits the cap is visible rather than quietly thinner.

    Args:
        session_id: Session whose batch is being appended to.
        entry: The envelope about to be written.

    Returns:
        True to skip the append, False to proceed.
    """
    if not BATCH_IS_CUMULATIVE:
        return False
    event_name = entry.get("hook_event_name", "") if isinstance(entry, dict) else ""
    if event_name in PROTECTED_HOOK_EVENTS:
        return False
    batch_file_path = get_batch_file(session_id)
    try:
        current_size = os.path.getsize(batch_file_path)
    except OSError:
        return False
    if current_size <= MAX_BATCH_RETAINED_BYTES:
        return False
    debug_log(
        f"append refused: session_id={session_id} envelope={event_name or '?'} "
        f"bytes={current_size} reason=batch-at-capacity-for-a-single-request"
    )
    return True


def _evict_batch_if_oversize(session_id: str, batch_file_size: int) -> None:
    """Shed low-value envelopes when a batch file has outgrown its budget.

    Called from every append site. The size check is a single number the caller
    already has, so the common path costs nothing: appends run on per-tool hooks
    thousands of times per session, and a read-parse-rewrite on each one would be
    orders of magnitude dearer than the append it guards.

    No upload interlock is needed here, unlike the runtimes that drain what they
    have delivered: this one re-sends the whole batch every time and removes
    nothing on success, so an upload in flight cannot have its drain redirected
    onto records it never sent. The read and the rewrite still happen under one
    exclusive lock, so two concurrent evictions cannot interleave.

    Args:
        session_id: Session whose batch file may need shedding.
        batch_file_size: Size of that file in bytes, as of the append.

    Returns:
        None.
    """
    if batch_file_size <= MAX_BATCH_RETAINED_BYTES:
        return
    with open(get_batch_file(session_id), "a+") as batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=True):
            evicted_count = _evict_locked_batch(
                batch_file_handle,
                BATCH_EVICTION_TARGET_BYTES,
                session_id,
            )
    if evicted_count:
        debug_log(
            f"_evict_batch_if_oversize: evicted={evicted_count} session_id={session_id}"
        )


def _terminate_partial_final_line(batch_file_path: str, locked_handle: Any) -> None:
    """Close off a half-written final line before appending after it.

    A process killed mid-append leaves a line with no terminating newline. The
    next append would concatenate onto it and both records would then read as one
    broken line, so the record already lost takes the next good one down with it —
    silently, with no error anywhere. Writing the missing newline first confines
    the loss to the torn record.

    The final byte is read through a separate read-only descriptor rather than
    through the handle, which works whether the caller opened the file append-only
    or append-and-read, and leaves the handle's position untouched either way.
    Python documents append-mode writes as landing at end-of-file only "on some
    Unix systems ... regardless of the current seek position", so nothing here
    depends on that. The caller holds the exclusive lock across the whole
    check-and-write, so no other process can append between the two.

    Args:
        batch_file_path: Path of the batch file being appended to.
        locked_handle: The exclusively locked append handle to write through.
    """
    try:
        file_size = os.fstat(locked_handle.fileno()).st_size
    except OSError:
        return
    if not file_size:
        return
    try:
        with open(batch_file_path, "rb") as probe_handle:
            probe_handle.seek(file_size - 1)
            if probe_handle.read(1) == b"\n":
                return
    except OSError:
        return
    locked_handle.write("\n")


def append_to_batch(session_id: str, entry: dict[str, Any]) -> None:
    """Append a single JSON line to the batch file for *session_id*."""
    if _append_is_refused(session_id, entry):
        return
    batch_file = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    batch_file_size = 0
    with open(batch_file, "a") as f:
        with _lock_file(f, exclusive=True):
            _terminate_partial_final_line(batch_file, f)
            f.write(line)
            f.flush()
            batch_file_size = f.tell()
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    _evict_batch_if_oversize(session_id, batch_file_size)


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
    """Empty a session's batch, leaving a zero-byte file.

    Truncates rather than unlinking. Unlinking detaches the inode while other
    processes may still hold it open, so their appends land on a file nothing
    will ever read again; truncating under the same lock every writer uses keeps
    them all on one file.

    Args:
        session_id: Session whose batch is being emptied.

    Returns:
        None.
    """
    rewrite_batch(session_id, [])


def rewrite_batch(session_id: str, entries: list[dict[str, Any]]) -> None:
    """Re-write a session's batch, replacing whatever is there.

    Opened ``a+`` and truncated *after* the lock is taken, never ``w``: opening
    for write empties the file at open time, before any lock is held, so a
    concurrent append that legitimately owns the lock loses its record. This
    runtime has three writers on the same file — the hook process, the detached
    refresh worker, and the size guard — so the ordering matters here more than
    anywhere else.

    Args:
        session_id: Session whose batch is being replaced.
        entries: The records to leave in the file, in order.

    Returns:
        None.
    """
    batch_file = get_batch_file(session_id)
    with open(batch_file, "a+") as f:
        with _lock_file(f, exclusive=True):
            f.seek(0)
            f.truncate()
            for entry in entries:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            # Flush inside the lock: truncate lands at once but the rewritten
            # lines sit in the buffer until close, which is after the lock is
            # released, so an append in that gap would be written over.
            f.flush()
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


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


def is_foreign_runtime_payload(payload: Any) -> bool:
    """Return True when a hook payload came from a different agent runtime.

    One runtime is known to discover and execute another's collector, handing it
    hooks from a session it does not serve. Nothing downstream can undo that: the
    collector stamps its own runtime on the batch, and the collector that uploads
    first is the one the session is filed under -- so a session belonging to one
    tool is recorded, whole, against another.

    Identification is by a marker key that only one runtime puts in its payloads.
    Absence proves nothing and is treated as "mine", so a runtime that stops
    sending its marker degrades to today's behaviour rather than losing capture.

    Args:
        payload: The hook payload as the runtime supplied it.

    Returns:
        True when the payload should be ignored by this collector.
    """
    if not isinstance(payload, dict):
        return False
    return any(marker in payload for marker in FOREIGN_RUNTIME_MARKERS)


def _entry_size(entry: dict[str, Any]) -> int:
    """Return the serialized byte size of one envelope.

    Matches how :func:`append_to_batch` writes the entry, so the figure here is
    the one that actually reaches the request body.

    Args:
        entry: The envelope to measure.

    Returns:
        The entry's length in bytes once JSON-encoded.
    """
    try:
        return len(json.dumps(entry, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        # Not free: the uploader serializes the whole body in one call, so a
        # single unserializable entry fails every request for the entire batch.
        # Scoring it above the request budget makes the prefix isolate it, and
        # the caller's undeliverable-envelope path then drops that one record
        # loudly instead of the batch stalling forever behind it.
        return MAX_UPLOAD_BYTES + 1


# Known limitation: a single turn larger than one request cannot be sent
# turn-aligned, because no cut inside it lands on a terminator. The chunks after
# the first then open mid-turn, and the collector only builds events for a turn
# it has seen the start of, so those events are discarded even though the
# request succeeds. Measured on real batches, a small percentage of turns are
# individually larger than the request budget. Closing this needs the collector
# to resolve a mid-turn request by its turn key the way it already resolves a
# turn-end; it cannot be fixed here alone.
def select_uploadable_prefix(
    entries: list[dict[str, Any]], max_bytes: int = MAX_UPLOAD_BYTES
) -> int:
    """Return how many leading entries can be sent in one request.

    Sending the whole batch every attempt is what makes an oversize batch
    permanent: the body is rejected, nothing drains, later hooks append, and the
    next attempt is larger still. Uploading a prefix converts that into
    incremental delivery, and :func:`drop_leading_entries` already drains by
    count, so the two compose without further bookkeeping.

    The cut is pulled back to a turn boundary where one exists. The backend
    attaches a tool event only when it can resolve the turn that event belongs
    to, so a turn split across two requests loses the events that land in the
    second one — silently, on both sides. Ending on a turn terminator keeps each
    request self-contained. When no boundary fits, the byte-greedy count stands:
    delivering a split turn still beats delivering nothing.

    The pullback is refused when it would give up most of what the budget allows.
    A turn whose own envelopes exceed one request has to be split somewhere, and
    the only boundary behind such a cut is the *previous* turn's terminator — so
    honouring it would ship a couple of leftover envelopes per request while the
    turn that overflowed never moves. Requiring the pullback to keep at least
    half the byte-greedy count guarantees every request makes real progress, and
    still aligns on turn boundaries in the ordinary case where a turn is far
    smaller than a request.

    Args:
        entries: The batch snapshot, in file order.
        max_bytes: Byte budget for the entries in one request.

    Returns:
        The number of leading entries to send. Always at least 1 when *entries*
        is non-empty — a single entry larger than the whole budget is still
        attempted, because refusing to send it would stall the batch forever.
        The server's 413 is the backstop for that case.
    """
    if not entries:
        return 0
    accumulated_bytes = 0
    selected_count = 0
    for entry in entries:
        entry_bytes = _entry_size(entry)
        if selected_count and accumulated_bytes + entry_bytes > max_bytes:
            break
        accumulated_bytes += entry_bytes
        selected_count += 1

    if selected_count == len(entries):
        return selected_count
    # Measured in bytes, not entries: what the budget spends is bytes, and a
    # cut of three entries can be most of the budget while a cut of thirty can
    # be almost none of it.
    minimum_progress_bytes = accumulated_bytes // 2
    boundary_bytes = 0
    for index in range(selected_count - 1, -1, -1):
        entry = entries[index]
        if not isinstance(entry, dict):
            continue
        if entry.get("hook_event_name", "") in TURN_TERMINAL_HOOK_EVENTS:
            # Scanning backwards, so this is the latest boundary there is: if it
            # is too far back to be worth taking, every earlier one is worse.
            boundary_bytes = sum(_entry_size(item) for item in entries[: index + 1])
            if boundary_bytes >= minimum_progress_bytes:
                return index + 1
            break
    return selected_count


# Marker left in place of text removed to make an envelope deliverable. Kept
# short and literal so it is obvious in the collected data that the value was
# cut here rather than produced this way.
OVERSIZE_TEXT_MARKER = "…[bloomfilter: truncated, envelope exceeded request budget]"

# How deep the capping walk will descend. A hook payload is data of unknown
# shape, and this runs several frames down inside an append, so an unbounded
# walk could exhaust the stack -- which would abort the append's eviction and
# leave the batch growing, the very thing eviction exists to stop. Anything
# deeper than this is replaced wholesale rather than descended into.
MAX_CAP_DEPTH = 40


# Shortest cap the shrinking walk will apply. Below this the text left is too
# short to identify what was cut, and an envelope that still does not fit at this
# cap is not going to be rescued by cutting further.
MIN_CAP_CHARS = 256

# What one capped string costs on the wire beyond the text kept: the marker, in
# encoded bytes rather than characters, plus the quotes and separator JSON adds.
_MARKER_ENCODED_BYTES = len(json.dumps(OVERSIZE_TEXT_MARKER).encode("utf-8")) - 2
_STRING_PUNCTUATION_BYTES = 3

# Ceiling on capping rounds. Each one is a full copy and a full measurement, and
# halving from any starting cap reaches the floor well inside this, so it only
# bounds a pathological input rather than a real one.
_MAX_SHRINK_ROUNDS = 24


def _string_stats(value: Any, depth: int = 0) -> tuple:
    """Return how many strings are inside *value* and how long the longest is.

    Walked once, so the shrinking cap can be chosen from the envelope's own
    contents instead of from the request budget. Bounded by the same depth limit
    as the capping walk so the two agree about what they can reach; anything
    deeper is not counted, exactly as it is not capped.

    Args:
        value: Any JSON-compatible value.
        depth: Current nesting depth.

    Returns:
        A (count, longest_length) tuple.
    """
    if isinstance(value, str):
        return 1, len(value)
    if depth >= MAX_CAP_DEPTH:
        return 0, 0
    if isinstance(value, dict):
        items = list(value.keys()) + list(value.values())
    elif isinstance(value, list):
        items = value
    else:
        return 0, 0
    count = 0
    longest = 0
    for item in items:
        item_count, item_longest = _string_stats(item, depth + 1)
        count += item_count
        if item_longest > longest:
            longest = item_longest
    return count, longest


def _cap_strings(value: Any, character_limit: int, depth: int = 0) -> Any:
    """Return *value* with every string inside it capped to *character_limit*.

    Walks nested dicts and lists so a long value buried in a tool result is
    reached as readily as one at the top level. Structure and identifiers are
    preserved and only text length changes -- including dictionary keys, because
    an envelope whose bulk sits in its keys could otherwise never be made to fit.

    Args:
        value: Any JSON-compatible value.
        character_limit: Longest string to keep uncut.
        depth: Current nesting depth; the walk stops descending past
            :data:`MAX_CAP_DEPTH`.

    Returns:
        The value with long strings replaced by a truncated copy plus a marker.
    """
    if isinstance(value, str):
        if len(value) <= character_limit:
            return value
        return value[:character_limit] + OVERSIZE_TEXT_MARKER
    if depth >= MAX_CAP_DEPTH:
        # Too deep to keep walking safely. Anything down here is replaced by the
        # marker rather than descended into, so the size still comes down.
        return OVERSIZE_TEXT_MARKER if isinstance(value, (dict, list)) else value
    if isinstance(value, dict):
        return {
            _cap_strings(key, character_limit, depth + 1): _cap_strings(
                item, character_limit, depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_cap_strings(item, character_limit, depth + 1) for item in value]
    return value


# Key left on a reduced envelope so the cut is visible in the collected data
# rather than looking like an envelope that arrived this sparse.
_REDUCED_MARKER_KEY = "bloomfilter_reduced"


def _reduce_to_identity(entry: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Return a stand-in small enough to send for an envelope nothing can carry.

    Last resort, for an envelope capping cannot bring under budget. Neither
    obvious option is available. Dropping it renumbers: on a runtime that
    supplies no per-turn key, a turn boundary's *position* is its identity, so
    removing one shifts every turn after it and the following turn's prompt lands
    against this turn's tool events. Keeping it stalls: no request containing it
    fits, so nothing behind it is ever delivered.

    So it stays where it is, reduced to the scalars that identify it, with the
    bulk replaced by a marker that is visibly not something the model produced.

    Args:
        entry: The envelope to reduce.
        max_bytes: Size the reduced envelope must fit within.

    Returns:
        A reduced copy, small enough to send.
    """
    def keep(value: Any) -> bool:
        if isinstance(value, str):
            return len(value) <= MIN_CAP_CHARS
        return isinstance(value, (int, float, bool)) or value is None

    reduced: dict[str, Any] = {}
    for key, value in entry.items():
        if keep(value):
            reduced[key] = value
        elif key == "payload" and isinstance(value, dict):
            reduced[key] = {k: v for k, v in value.items() if keep(v)}
            reduced[key][_REDUCED_MARKER_KEY] = OVERSIZE_TEXT_MARKER
        else:
            reduced[key] = OVERSIZE_TEXT_MARKER
    if _entry_size(reduced) <= max_bytes:
        return reduced
    # Even the identifiers do not fit, which takes thousands of them. Keep what
    # names the envelope and nothing else, so its position is still held.
    return {
        "hook_event_name": entry.get("hook_event_name", ""),
        _REDUCED_MARKER_KEY: OVERSIZE_TEXT_MARKER,
    }


def _shrink_entry(entry: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Return a copy of *entry* small enough to send, or the entry unchanged.

    Used for the envelopes that must not be dropped whatever their size. Halves
    the text budget until the envelope fits, so identifiers, event name and
    structure survive while the bulk -- which is always long text -- is cut.
    Stops early once a round stops making the envelope smaller, because each
    round is a full copy and a full measurement: an envelope made of very many
    medium-length values cannot be shrunk by capping length, and grinding
    through every remaining round would burn the hook's budget to no effect.

    Args:
        entry: The envelope to shrink.
        max_bytes: Size the shrunk envelope must fit within.

    Returns:
        A shrunk copy, or the original entry when it cannot be shrunk.
    """
    string_count, longest_string = _string_stats(entry)

    # First cap: the share of the budget each capped string can afford, counting
    # what capping *adds* as well as what it keeps. The marker is measured in
    # encoded bytes, not characters -- it opens with an ellipsis, which JSON
    # escapes to six bytes -- and each string also carries its quotes and
    # separator. Under-counting either lands the first round a few bytes over
    # budget on exactly the envelopes this exists for.
    per_string_cost = _MARKER_ENCODED_BYTES + _STRING_PUNCTUATION_BYTES
    affordable = max_bytes // max(string_count, 1) - per_string_cost

    # Never above the longest string, or the round cuts nothing; never below the
    # floor, or the text left cannot show what was cut. A zero here means every
    # string sits below the depth the capping walk descends to, where whole
    # containers are replaced instead -- so the floor is the right starting cap.
    character_limit = max(MIN_CAP_CHARS, min(affordable, longest_string or MIN_CAP_CHARS))

    # Halve on any round that does not fit, whether or not it made progress. A
    # round that changes nothing does not mean the envelope cannot be cut: the
    # cap may simply still be above every string, and multi-byte text inflates
    # under JSON escaping by up to twelve times, so a cap that looks generous in
    # characters can be far too generous in bytes. Treating no-progress as proof
    # of impossibility is what left these envelopes untouched.
    for _ in range(_MAX_SHRINK_ROUNDS):
        shrunk = _cap_strings(entry, character_limit)
        if _entry_size(shrunk) <= max_bytes:
            return shrunk
        if character_limit <= MIN_CAP_CHARS:
            break
        character_limit = max(MIN_CAP_CHARS, character_limit // 2)
    return entry


def shed_undeliverable_entries(
    entries: list[dict[str, Any]], max_bytes: int = MAX_UPLOAD_BYTES
) -> list[dict[str, Any]]:
    """Drop envelopes too large to fit in any single request.

    One envelope bigger than a whole request can never be delivered: no prefix
    that contains it fits, so on a runtime that re-sends from the first record
    every upload stops short of it and everything behind it is stranded for
    good. Removing it is real data loss — it is almost always one enormous tool
    result — but the alternative is losing the rest of the session with it.

    Turn-start envelopes are the one exception: on a runtime that supplies no
    per-turn key they are the only marker of where one turn ends and the next
    begins, so one going missing shifts every turn after it. Those are shrunk
    instead — the long text inside them is cut until the envelope fits, which
    keeps the turn, its identifiers and its position while shedding only the
    bulk.

    Args:
        entries: The batch snapshot, in file order.
        max_bytes: Largest size a single envelope may have and still be sent.

    Returns:
        The entries that can still be delivered, in their original order.
    """
    undeliverable_indexes = set()
    shrunk_entries = {}
    for index, entry in enumerate(entries):
        if _entry_size(entry) <= max_bytes:
            continue
        event_name = entry.get("hook_event_name", "") if isinstance(entry, dict) else ""
        if event_name in PROTECTED_HOOK_EVENTS:
            shrunk = _shrink_entry(entry, max_bytes)
            shrunk_size = _entry_size(shrunk)
            if shrunk_size <= max_bytes:
                shrunk_entries[index] = shrunk
                debug_log(
                    "shed_undeliverable_entries: shrank an oversize turn-start "
                    f"envelope envelope={event_name} bytes={_entry_size(entry)} "
                    f"shrunk_bytes={shrunk_size} "
                    "reason=dropping-it-would-renumber-later-turns"
                )
                continue
            # Shrinking failed, so the envelope is reduced to its identifiers
            # instead. It is not dropped: on a runtime with no per-turn key a
            # boundary's position is its identity, and removing one renumbers
            # every turn behind it. It is not kept either -- no request holding
            # it fits, so the batch would never advance past it.
            shrunk_entries[index] = _reduce_to_identity(entry, max_bytes)
            debug_log(
                "shed_undeliverable_entries: reduced an oversize turn-start "
                f"envelope to its identifiers envelope={event_name} "
                f"bytes={_entry_size(entry)} "
                f"reduced_bytes={_entry_size(shrunk_entries[index])} "
                "reason=could-not-be-shrunk-and-dropping-it-would-renumber"
            )
            continue
        undeliverable_indexes.add(index)
        debug_log(
            "shed_undeliverable_entries: dropping an undeliverable envelope "
            f"envelope={event_name or '?'} bytes={_entry_size(entry)} "
            "reason=single-envelope-exceeds-request-budget"
        )
    if not undeliverable_indexes and not shrunk_entries:
        return entries
    return [
        shrunk_entries.get(index, entry)
        for index, entry in enumerate(entries)
        if index not in undeliverable_indexes
    ]


def evict_low_value_entries(
    entries: list[dict[str, Any]],
    max_bytes: int = MAX_UPLOAD_BYTES,
    eviction_limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Drop the least valuable envelopes until the batch fits *max_bytes*.

    Eviction is priority-ordered rather than by size or age alone. Tool traffic
    dominates the bytes but carries no turn identity, so it goes first, oldest
    first; ``SubagentStop`` goes only once that is exhausted; and the envelopes
    naming prompts and totals are never dropped. A batch reduced to its
    protected entries may still exceed the budget — that is deliberate, and
    :func:`select_uploadable_prefix` splits it across requests instead.

    Args:
        entries: The batch snapshot, in file order.
        max_bytes: Byte budget the retained entries should fit within.
        eviction_limit: Exclusive upper index eviction may touch. Defaults to
            the whole batch; a runtime that re-sends everything passes the point
            past which removing a record would retract one already delivered.

    Returns:
        The retained entries in their original relative order.
    """
    if eviction_limit is None:
        eviction_limit = len(entries)
    total_bytes = sum(_entry_size(entry) for entry in entries)
    if total_bytes <= max_bytes:
        return entries
    # Protected envelopes are never shed, so once they alone exceed the budget no
    # amount of eviction can reach it. Shedding anyway would destroy the newest
    # tool traffic — including the envelope just appended — buy nothing, and pay
    # a full rewrite on every subsequent append. Leave the batch as it is and let
    # the upload prefix drain it down instead.
    protected_bytes = sum(
        _entry_size(entry)
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("hook_event_name") in PROTECTED_HOOK_EVENTS
    )
    if protected_bytes > max_bytes:
        debug_log(
            "evict_low_value_entries: skipped, protected envelopes alone "
            f"exceed the budget protected_bytes={protected_bytes} "
            f"max_bytes={max_bytes}"
        )
        return entries

    # Oldest-first within each tier, and the whole first tier before the second.
    first_tier_indexes = []
    second_tier_indexes = []
    for index, entry in enumerate(entries):
        if index >= eviction_limit:
            break
        # A batch line only has to be valid JSON to be read back, so a truncated
        # write can leave a bare scalar here. Treat anything that is not an
        # envelope as first to go rather than letting it raise — an exception
        # would be swallowed by the hook's guard and silently disable eviction
        # for the rest of the session.
        event_name = entry.get("hook_event_name", "") if isinstance(entry, dict) else ""
        if event_name in PROTECTED_HOOK_EVENTS:
            continue
        if event_name in DEFERRED_HOOK_EVENTS:
            second_tier_indexes.append(index)
        else:
            first_tier_indexes.append(index)
    evictable_indexes = first_tier_indexes + second_tier_indexes

    dropped_indexes = set()
    for index in evictable_indexes:
        if total_bytes <= max_bytes:
            break
        dropped_indexes.add(index)
        total_bytes -= _entry_size(entries[index])

    return [
        entry for index, entry in enumerate(entries) if index not in dropped_indexes
    ]


def _evict_locked_batch(
    batch_file_handle: IO[str], max_bytes: int, session_id: str
) -> int:
    """Shed low-value envelopes from a batch file whose lock is already held.

    Takes the open handle rather than a session id precisely so the caller's
    exclusive lock spans the read and the rewrite. Doing this as a separate
    read-then-write would leave a window in which a concurrent upload drains
    records that this call has already read, and the rewrite would restore them
    to be uploaded — and counted — a second time.

    Args:
        batch_file_handle: Handle opened ``a+`` with an exclusive lock held.
        max_bytes: Byte budget the retained entries should fit within.
        session_id: Session the batch belongs to, used to read how much of it
            has already been delivered.

    Returns:
        How many entries were evicted. Zero leaves the file untouched.
    """
    batch_file_handle.seek(0)
    existing_entries = []
    for raw_line in batch_file_handle.readlines():
        is_record, decoded_entry = _decode_batch_line(raw_line)
        if is_record:
            existing_entries.append(decoded_entry)
    # Undeliverable envelopes go first and unconditionally: they are not
    # merely low value, they are impossible to send at any batch size, and
    # a size-driven eviction that happened to keep one would leave the
    # batch permanently blocked behind it.
    deliverable_entries = shed_undeliverable_entries(existing_entries)
    # A runtime that re-sends the whole batch may only shed records belonging to
    # turns it has already closed with the collector; anything newer would be
    # retracted rather than merely forgotten.
    eviction_limit = len(deliverable_entries)
    if BATCH_IS_CUMULATIVE:
        eviction_limit = _safe_eviction_limit(
            deliverable_entries, read_delivered_prefix(session_id)
        )
    retained_entries = evict_low_value_entries(
        deliverable_entries, max_bytes=max_bytes, eviction_limit=eviction_limit
    )
    evicted_count = len(existing_entries) - len(retained_entries)
    # Shrinking an envelope changes no count, so compare content as well: a
    # rewrite discarded because "nothing was evicted" would leave the oversize
    # envelope on disk to be shrunk and thrown away again on every append.
    if not evicted_count and retained_entries == existing_entries:
        return 0
    batch_file_handle.seek(0)
    batch_file_handle.truncate()
    for retained_entry in retained_entries:
        batch_file_handle.write(
            json.dumps(retained_entry, separators=(",", ":")) + "\n"
        )
    # Flush while the lock is still held. truncate() takes effect at once but the
    # rewritten lines only leave Python's buffer at close, which happens after the
    # lock is released -- so an append that legitimately takes the lock in that
    # gap would write at the truncated end and have this tail land on top of it,
    # corrupting both records.
    batch_file_handle.flush()
    return evicted_count


def sweep_stale_batches(
    max_age_seconds: int = BATCH_MAX_AGE_SECONDS,
    current_session_id: str = "",
) -> int:
    """Delete batch files older than *max_age_seconds* and report the count.

    :func:`cleanup_session_batch` only removes a batch that has been fully
    drained, so any batch whose session died mid-flight is retained forever by
    design. This is the only path that bounds the directory.

    Intended to run on ``SessionStart`` only: a scandir over a few hundred
    entries costs about a millisecond, which is negligible once per session but
    unacceptable on ``PreToolUse``, which fires thousands of times.

    Args:
        max_age_seconds: Age beyond which a batch file is removed.

    Returns:
        How many files were deleted. Zero when the directory is absent.
    """
    batch_dir_path = get_batch_dir()
    if not os.path.isdir(batch_dir_path):
        return 0
    cutoff_time = time.time() - max_age_seconds
    removed_count = 0
    try:
        batch_file_names = os.listdir(batch_dir_path)
    except OSError:
        return 0
    for batch_file_name in batch_file_names:
        # Batch files only. A ``.upload`` slot file must never be swept on age:
        # upload_slot only opens it, never writes, so its mtime is frozen at
        # creation and goes stale while the session is still live — and deleting
        # a held lock lets a second uploader in, which is the double-drain that
        # slot exists to prevent. cleanup_session_batch owns their removal.
        if not batch_file_name.endswith(".jsonl"):
            continue
        # A resumed session's own batch is not stale, whatever its age says: the
        # sweep runs at session start, before this session has appended
        # anything, so its mtime is still the last-active time from the previous
        # sitting. Sweeping it would delete records the coming turn would ship.
        if current_session_id and batch_file_name == f"{current_session_id}.jsonl":
            continue
        batch_file_path = os.path.join(batch_dir_path, batch_file_name)
        try:
            if os.path.getmtime(batch_file_path) >= cutoff_time:
                continue
            os.unlink(batch_file_path)
        except OSError:
            # A batch removed by another process, or one we cannot delete, must
            # never fail the hook that is sweeping.
            continue
        removed_count += 1
        for sidecar_suffix in (".upload", ".sent"):
            with contextlib.suppress(OSError):
                os.unlink(batch_file_path + sidecar_suffix)
    if removed_count:
        debug_log(
            f"sweep_stale_batches: removed={removed_count} "
            f"max_age_seconds={max_age_seconds}"
        )
    return removed_count


def upload_batch(api_url: str, api_key: str, payload: dict[str, Any]) -> str:
    """POST raw hook batch to the Bloomfilter API.

    Validates the URL scheme up front: only http/https are allowed.

    Network interactions are logged to <bloomfilter-config>/debug.log: the
    request URL + session_id + hook count + payload bytes, the response
    status + truncated body, and any HTTPError / URLError / unexpected
    exception.

    Returns:
        UPLOAD_OK when the server answered 2xx, meaning the records are safe to
        drain. UPLOAD_TOO_LARGE when it answered 413, meaning the caller must
        send fewer records rather than retry this body — an identical oversize
        request can never succeed, so collapsing 413 into the generic failure is
        what makes an oversize batch permanent. UPLOAD_FAILED for an invalid
        URL, an unserializable payload, a transport error, or any other non-2xx
        status; the caller keeps the records and retries later.
    """
    parsed = urllib.parse.urlparse(api_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return UPLOAD_FAILED

    # Accessing .port raises for a malformed port (non-numeric or out of range),
    # so validate it here rather than letting it throw further down.
    try:
        parsed.port  # noqa: B018 — evaluated for its validation side effect
        port_is_valid = True
    except ValueError:
        port_is_valid = False

    if not port_is_valid:
        debug_log("upload_batch: skipped — api_url has an unusable port")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return UPLOAD_FAILED

    # Never send the API token, the prompts or the tool output over cleartext
    # HTTP. The URL comes from the environment or a config file, so anything able
    # to set either could otherwise redirect a whole session's telemetry to a
    # host of its choosing, readable by everything on the path. http stays
    # allowed for loopback only, so local development keeps working.
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        debug_log("upload_batch: skipped — refusing cleartext (non-loopback) api_url")
        print(
            "[bloomfilter] Upload skipped: Bloomfilter API URL must use HTTPS.",
            file=sys.stderr,
        )
        return UPLOAD_FAILED

    # Refuse a key that cannot go in a header. The HTTP client raises on such a
    # value and its exception message *is* the value, so letting it through
    # would print the key into the debug log and the terminal. Checked here as
    # well as at resolve time because this function is also called directly.
    api_key = _sanitize_api_key(api_key)
    if not api_key:
        debug_log("upload_batch: refusing to send with an unusable API key")
        return UPLOAD_FAILED

    url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    hook_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0

    try:
        # Compact separators, matching how entries are measured on the way in
        # and written to the batch file. Default separators add ", " and ": ",
        # inflating the body by up to 1.5x for structured payloads — enough for a
        # batch that measured under the client cap to still be rejected as too
        # large, which is the failure that cap exists to prevent.
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} error={type(exc).__name__}: {exc}"
        )
        return UPLOAD_FAILED

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
                # Also headers, not only the body: a request refused for
                # its size is never parsed, so the body's copy is exactly
                # what cannot be read when the sender matters most.
                # Version alone does not identify a build -- several
                # share one -- so the source travels with it.
                "X-Plugin-Version": PLUGIN_VERSION,
                "X-Plugin-Source": DEBUG_LOG_TAG,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_S) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
        debug_log(
            f"upload_batch: response status={status} session_id={session_id} "
            f"body={body[:500]!r}"
        )
        if not 200 <= status < 300:
            print(f"[bloomfilter] Upload response status: {status}", file=sys.stderr)
        return UPLOAD_OK if 200 <= status < 300 else UPLOAD_FAILED
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
        # A 413 is expected control flow now, not an error to report: the
        # caller answers it by sending a smaller prefix. Printing it would put
        # two lines of HTTP error text in the user's terminal on a path that
        # recovers by itself. It stays in the debug log above.
        if exc.code == 413:
            return UPLOAD_TOO_LARGE
        message = f"[bloomfilter] Upload failed with HTTP {exc.code}"
        if reason:
            message += f" {reason}"
        print(message, file=sys.stderr)
        if err_body:
            print(
                f"[bloomfilter] Upload response body: {err_body[:500]}", file=sys.stderr
            )
        return UPLOAD_FAILED
    except urllib.error.URLError as exc:
        debug_log(
            f"upload_batch: URLError session_id={session_id} reason={exc.reason!r}"
        )
        print(f"[bloomfilter] Upload failed: {exc.reason}", file=sys.stderr)
        return UPLOAD_FAILED
    except Exception as exc:
        debug_log(
            f"upload_batch: error session_id={session_id} "
            f"type={type(exc).__name__} message={exc!s}"
        )
        print(f"[bloomfilter] Upload failed: {exc}", file=sys.stderr)
        return UPLOAD_FAILED


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
