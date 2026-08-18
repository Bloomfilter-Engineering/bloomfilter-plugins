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
from typing import Optional, IO, Any, Iterator, TextIO

if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION: str = "0.3.0"
_SUBAGENT_FIELD_CAP: int = 10_000
DEFAULT_API_URL: str = "https://api.bloomfilter.app"
DEBUG_LOG_NAME: str = "debug.log"
DEBUG_LOG_TAG: str = "codex"  # disambiguates plugins sharing the same log dir

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
    """Resolve the API key from env var or user config only."""
    api_key_from_env = os.environ.get("BLOOMFILTER_API_KEY", "")
    if api_key_from_env:
        return _sanitize_api_key(api_key_from_env)

    user_config_path = os.path.join(get_config_dir(), "config.json")
    return _sanitize_api_key(read_json_config(user_config_path, "api_key"))


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
    """Read a JSON hook payload from stdin.

    Uses utf-8-sig on Windows so a leading BOM is stripped — PowerShell pipes to
    a native executable can prefix stdin with a UTF-8 BOM on Windows PowerShell
    5.1, which would otherwise break json.loads.
    """
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8-sig")
    raw_payload = sys.stdin.read()
    return json.loads(raw_payload) if raw_payload.strip() else {}


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
    """Return path to the JSONL batch file for session_id."""
    safe_session_id = os.path.basename(session_id)
    if not safe_session_id or safe_session_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_session_id}.jsonl")


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


def append_to_batch(session_id: str, entry: dict[str, Any]) -> None:
    """Append one JSON object to the session batch file."""
    if _append_is_refused(session_id, entry):
        return
    batch_file_path = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    batch_file_size = 0
    with open(batch_file_path, "a") as batch_file:
        with _lock_file(batch_file, exclusive=True):
            batch_file.write(line)
            batch_file.flush()
            batch_file_size = batch_file.tell()
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    _evict_batch_if_oversize(session_id, batch_file_size)


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
    character_limit = max_bytes // 2
    previous_size = _entry_size(entry)
    while character_limit >= 256:
        shrunk = _cap_strings(entry, character_limit)
        shrunk_size = _entry_size(shrunk)
        if shrunk_size <= max_bytes:
            return shrunk
        if shrunk_size >= previous_size:
            break
        previous_size = shrunk_size
        character_limit //= 2
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
            shrunk_entries[index] = _shrink_entry(entry, max_bytes)
            debug_log(
                "shed_undeliverable_entries: shrank an oversize turn-start "
                f"envelope envelope={event_name} bytes={_entry_size(entry)} "
                f"shrunk_bytes={_entry_size(shrunk_entries[index])} "
                "reason=dropping-it-would-renumber-later-turns"
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
    """POST a raw hook batch to the Bloomfilter API.

    Validates the URL scheme up front: only http/https are allowed.

    Network interactions are logged to <plugin-data>/debug.log: the request
    URL + session_id + hook count + payload bytes, the response status +
    truncated body, and any HTTPError / URLError / unexpected exception.

    Returns:
        UPLOAD_OK when the server answered 2xx, meaning the records are safe to
        drain. UPLOAD_TOO_LARGE when it answered 413, meaning the caller must
        send fewer records rather than retry this body — an identical oversize
        request can never succeed, so collapsing 413 into the generic failure is
        what makes an oversize batch permanent. UPLOAD_FAILED for an invalid
        URL, an unserializable payload, a transport error, or any other non-2xx
        status; the caller keeps the records and retries later.
    """
    parsed_url = urllib.parse.urlparse(api_url or "")
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return UPLOAD_FAILED

    # Accessing .port raises for a malformed port (non-numeric or out of range),
    # so validate it here rather than letting it throw further down.
    try:
        parsed_url.port  # noqa: B018 — evaluated for its validation side effect
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
    if parsed_url.scheme == "http" and parsed_url.hostname not in {
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

    full_url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    hook_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0

    try:
        # Compact separators, matching how entries are measured on the way in
        # and written to the batch file. Default separators add ", " and ": ",
        # inflating the body by up to 1.5x for structured payloads — enough for a
        # batch that measured under the client cap to still be rejected as too
        # large, which is the failure that cap exists to prevent.
        request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} error={type(exc).__name__}: {exc}"
        )
        return UPLOAD_FAILED

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
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_S) as response:
            status_code = response.status
            response_body = response.read().decode("utf-8", errors="replace")
        debug_log(
            f"upload_batch: response status={status_code} session_id={session_id} "
            f"body={response_body[:500]!r}"
        )
        if not 200 <= status_code < 300:
            print(
                f"[bloomfilter] Upload response status: {status_code}", file=sys.stderr
            )
        return UPLOAD_OK if 200 <= status_code < 300 else UPLOAD_FAILED
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
        # A 413 is expected control flow now, not an error to report: the
        # caller answers it by sending a smaller prefix. Printing it would put
        # two lines of HTTP error text in the user's terminal on a path that
        # recovers by itself. It stays in the debug log above.
        if http_error.code == 413:
            return UPLOAD_TOO_LARGE
        message = f"[bloomfilter] Upload failed with HTTP {http_error.code}"
        if reason:
            message += f" {reason}"
        print(message, file=sys.stderr)
        if error_body:
            print(
                f"[bloomfilter] Upload response body: {error_body[:500]}",
                file=sys.stderr,
            )
        return UPLOAD_FAILED
    except urllib.error.URLError as url_error:
        debug_log(
            f"upload_batch: URLError session_id={session_id} reason={url_error.reason!r}"
        )
        print(f"[bloomfilter] Upload failed: {url_error.reason}", file=sys.stderr)
        return UPLOAD_FAILED
    except Exception as error:
        debug_log(
            f"upload_batch: error session_id={session_id} "
            f"type={type(error).__name__} message={error!s}"
        )
        print(f"[bloomfilter] Upload failed: {error}", file=sys.stderr)
        return UPLOAD_FAILED


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
