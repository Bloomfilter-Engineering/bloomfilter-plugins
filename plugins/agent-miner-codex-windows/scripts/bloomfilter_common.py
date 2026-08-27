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
from typing import IO, Any, Iterator, TextIO

if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

# codex_rollout sits beside this module; every entrypoint puts the scripts dir on
# sys.path before importing either, so a module-level import resolves.
from codex_rollout import parse_transcript

PLUGIN_VERSION: str = "0.2.1"
_SUBAGENT_FIELD_CAP: int = 10_000
DEFAULT_API_URL: str = "https://api.bloomfilter.app"
DEBUG_LOG_NAME: str = "debug.log"
DEBUG_LOG_TAG: str = "codex-windows"  # disambiguates plugins sharing the same log dir

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
# execute each other's collectors — one editor runs another's hook scripts from
# its plugin cache — so a collector can be handed a session it does not serve.
# It has no way to tell from the event name alone, because the names it is given
# are its own. These are payload keys observed in every envelope from another
# runtime and in none from this one, so their presence identifies the sender.
FOREIGN_RUNTIME_MARKERS = frozenset({"cursor_version"})

# Envelopes carrying turn identity and token counts. They are a small fraction of
# envelope count, while tool-output envelopes dominate the bytes, so shedding by
# size alone would discard precisely the records the upload exists to deliver.
# Never evicted.
PROTECTED_HOOK_EVENTS = frozenset(
    {"UserPromptSubmit", "Stop", "SessionStart", "SessionEnd"}
)

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
    CLAUDE_PLUGIN_DATA pointing at a plugin-scoped cache dir, but we
    deliberately ignore those so debug.log lives next to the user's
    config.json and batches/ — one well-known place to look for diagnostics.

    Returns:
        Path to the directory debug.log is written in — absolute on the same
        condition as :func:`get_config_dir`.
    """
    return get_config_dir()


def debug_log(message: str) -> None:
    """Append a timestamped line to <bloomfilter-config>/debug.log.

    Always writes — silent on failure. Intended for ops/diagnostic visibility
    of upload events without polluting Codex's TUI stderr.

    Args:
        message: Text to record. Written verbatim after the timestamp and the
            plugin tag, so it must never contain the API key or any secret.
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
    """Return the Bloomfilter config directory for the current platform.

    Returns:
        The Bloomfilter config directory for this platform. Absolute whenever
        the home directory resolves — which covers every case observed in
        practice, including a relative ``XDG_CONFIG_HOME``/``APPDATA``, since
        the fallback discards the env var and rebuilds from the home directory.
        NOT guaranteed absolute in one residual case: if ``expanduser`` itself
        returns ``~`` unchanged (no HOME and no passwd entry, as in a container
        running an unmapped UID) the fallback rebuilds from that same ``~`` and
        cannot recover.
    """
    system_name = platform.system()
    if system_name == "Windows":
        # A variable that is set but empty must fall back, not resolve to "".
        # os.environ.get returns the default only when the key is absent, so an
        # empty value would make this path relative and land the batch — prompts
        # and reasoning text in cleartext — in the hook's working directory,
        # which is the user's project.
        appdata_dir = os.environ.get("APPDATA") or os.path.expanduser("~")
        if not os.path.isabs(appdata_dir):
            appdata_dir = os.path.expanduser("~")
        return os.path.join(appdata_dir, "bloomfilter")
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    if not os.path.isabs(xdg_config_home):
        xdg_config_home = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg_config_home, "bloomfilter")


def secure_makedirs(directory_path: str) -> None:
    """Create directories with owner-only permissions on Unix.

    Args:
        directory_path: Directory to create. Missing parents are created too. An
            existing directory is reused rather than raising — but its mode is
            still narrowed to 0700 on Unix, so calling this on a directory that
            was deliberately left group- or world-readable will tighten it.
    """
    os.makedirs(directory_path, exist_ok=True)
    if platform.system() != "Windows":
        os.chmod(directory_path, stat.S_IRWXU)  # 0o700


def read_json_config(config_path: str, key: str, default: str = "") -> str:
    """Safely read a single key from a JSON config file.

    Opens with utf-8-sig so a leading BOM is stripped — `Set-Content -Encoding
    UTF8` on Windows PowerShell 5.1 writes a BOM, and the README's Windows setup
    snippet uses exactly that, so user-created configs land here BOM-prefixed.

    Args:
        config_path: Config file to read.
        key: Top-level key to look up.
        default: Value returned when the file is missing or unreadable, the key
            is absent, or the stored value is not a non-empty string.

    Returns:
        The stored string, or *default* when there is no usable value. Never
        raises: a malformed config degrades to the default rather than failing
        the hook that is reading it.
    """
    try:
        with open(config_path, "r", encoding="utf-8-sig") as config_file:
            value = json.load(config_file).get(key, default)
            return value if isinstance(value, str) and value else default
    except Exception:
        return default


def bootstrap_config(plugin_root: str) -> str:
    """Create the user config from the plugin template if it does not exist.

    Args:
        plugin_root: Directory holding the packaged ``bloomfilter.config.json``
            template that seeds a first-run config.

    Returns:
        Path to the user config file, whether it already existed or was created
        by this call — absolute on the same condition as
        :func:`get_config_dir`.
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
        # Never log or echo the value — only that one was rejected, and why.
        debug_log(
            "resolve_api_key: rejected a key containing control characters "
            f"(length={len(key)})"
        )
        return ""
    return key


def resolve_api_key() -> str:
    """Resolve the API key from env var or user config only.

    Returns:
        The sanitized key, or '' when none is configured or the configured one
        cannot safely go in a header.
    """
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

    Returns:
        The configured base URL, or :data:`DEFAULT_API_URL` when neither the
        environment nor the user config supplies one.
    """
    api_url_from_env = os.environ.get("BLOOMFILTER_URL", "")
    if api_url_from_env:
        return api_url_from_env

    user_config_path = os.path.join(get_config_dir(), "config.json")
    user_url = read_json_config(user_config_path, "url")
    if user_url:
        return user_url

    return DEFAULT_API_URL


def read_payload() -> Any:
    """Read a JSON hook payload from stdin.

    Returns:
        The parsed JSON value. ``{}`` ONLY when stdin is empty or blank:
        malformed JSON is not swallowed here, ``json.loads`` raises
        JSONDecodeError. This runtime's entrypoint catches it WITHOUT logging,
        so a malformed payload is an invisible no-op — unlike the other
        collectors, which record it in debug.log.
    """
    if platform.system() == "Windows":
        # utf-8-sig: PowerShell 5.1 pipes can prefix stdin with a UTF-8 BOM.
        sys.stdin.reconfigure(encoding="utf-8-sig")
    raw_payload = sys.stdin.read()
    return json.loads(raw_payload) if raw_payload.strip() else {}


def _resolve_git_executable() -> str:
    """Return a git executable path if available, or '' if none is found.

    Returns:
        Absolute path to a usable git executable, or '' when none is found.
    """
    git = shutil.which("git")
    # shutil.which can return a cwd-relative hit, and a process started
    # without an explicit executable path searches the current directory
    # before PATH on some platforms — so an opened folder shipping its own
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
    """Return the current git branch, or '' on failure.

    Args:
        project_dir: Working directory the branch is read from. Passed to git
            with ``-C``, so it need not be the process's own cwd.

    Returns:
        The branch name, or '' when git is unavailable, the directory is not a
        repository, or the command fails or times out.
    """
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
        """Acquire an flock on an open file, release on exit.

        Args:
            file_handle: An open file object whose descriptor is locked.
            exclusive: Request an exclusive lock rather than a shared one.

        Yields:
            None. The lock is released on exit.
        """
        lock_operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(file_handle, lock_operation)
        try:
            yield
        finally:
            fcntl.flock(file_handle, fcntl.LOCK_UN)

else:

    @contextlib.contextmanager
    def _lock_file(file_handle: TextIO, exclusive: bool = True) -> Iterator[None]:
        """Cross-process byte-range lock on Windows via msvcrt.locking.

        Args:
            file_handle: An open file object whose descriptor is locked.
            exclusive: Request an exclusive lock rather than a shared one.

        Yields:
            None. The lock is released on exit.
        """
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
    """Return and create the Bloomfilter hook batch directory.

    Returns:
        Path to the batch directory, which is created if absent — absolute on
        the same condition as :func:`get_config_dir`.
    """
    batch_dir = os.path.join(get_config_dir(), "batches")
    # Refuse a symlinked batch directory. The config root is taken from the
    # environment, so anything able to set that for the editor's child processes
    # could point this at a directory of its choosing — and the age sweep below
    # deletes files there. A real directory is the only thing safe to sweep.
    if os.path.islink(batch_dir):
        debug_log(f"get_batch_dir: refusing symlinked batch dir {batch_dir}")
        raise RuntimeError("batch directory must not be a symlink")
    secure_makedirs(batch_dir)
    return batch_dir


def get_batch_file(session_id: str) -> str:
    """Return path to the JSONL batch file for session_id.

    Args:
        session_id: Session whose batch file path is built.

    Returns:
        Path to that session's JSONL batch file — absolute on the same
        condition as :func:`get_config_dir`.
    """
    safe_session_id = os.path.basename(session_id)
    if not safe_session_id or safe_session_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_session_id}.jsonl")


def _delivered_marker_path(session_id: str) -> str:
    """Return the path of the sidecar recording how much of a batch was sent.

    Args:
        session_id: Session the batch belongs to.

    Returns:
        Path to the marker file — absolute on the same condition as
        :func:`get_config_dir`.
    """
    return get_batch_file(session_id) + ".sent"


def _batch_file_size(session_id: str) -> int:
    """Return the batch file's size in bytes.

    Args:
        session_id: Session whose batch file is being measured.

    Returns:
        The size on disk, or 0 when there is no file yet.
    """
    try:
        return os.path.getsize(get_batch_file(session_id))
    except OSError:
        return 0


def _batch_record_count(session_id: str) -> int:
    """Return how many records the batch file currently holds.

    Counted by line rather than parsed: the caller needs a bound, and parsing
    the whole batch on every read would cost more than the guard it feeds.

    Args:
        session_id: Session whose batch file is being counted.

    Returns:
        The number of records on disk, or 0 when there is no file yet.
    """
    try:
        with open(get_batch_file(session_id), "rb") as batch_file_handle:
            return sum(1 for line in batch_file_handle if line.strip())
    except OSError:
        return 0


def _read_delivered_marker(session_id: str) -> int:
    """Return the delivered count exactly as recorded, without clamping.

    The stored number is what the decrement has to work from. Reading it back
    through the clamp would subtract a removal twice: the clamp already lowers
    the value to the records now on disk, and the caller then subtracts the
    same removal again.

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
    recorded = _read_delivered_marker(session_id)
    if not recorded:
        return 0
    # Clamped against what is actually on disk. The mark only ever rises, so
    # anything that shortens the file without lowering it — a truncation, a
    # rewrite, an eviction on a build with no decrement path — leaves a count
    # describing records that no longer exist, and the next sitting's fresh
    # records are then measured against it and read as already delivered.
    return min(recorded, _batch_record_count(session_id))


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
    records have already been delivered safely — so a session that appends again
    has its fresh, unsent records treated as already sent.

    Args:
        session_id: Session whose marker is lowered.
        removed_record_count: How many records were removed from the head.

    Returns:
        None.
    """
    if removed_record_count <= 0:
        return
    remaining = max(_read_delivered_marker(session_id) - removed_record_count, 0)
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
    # Read before the lock is taken. Counting the file's records
    # re-opens it, and on Windows the lock below is mandatory, so
    # doing this from inside the locked region fails and the count
    # reads as zero — which would bound eviction to nothing.
    delivered_count = read_delivered_prefix(session_id)
    with open(get_batch_file(session_id), "a+") as batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=True):
            evicted_count = _evict_locked_batch(
                batch_file_handle,
                BATCH_EVICTION_TARGET_BYTES,
                session_id,
                delivered_count,
            )
    # Eviction may only touch records the collector has already
    # accepted, so everything it removed was inside the delivered
    # prefix. Leaving the mark where it was would make the records
    # that slid into those positions read as already sent.
    _reduce_delivered_prefix(session_id, evicted_count)
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
    """Append one JSON object to the session batch file.

    Args:
        session_id: Session whose batch file the entry is appended to.
        entry: The envelope to append, serialized as one JSON line.

    Returns:
        None.
    """
    if _append_is_refused(session_id, entry):
        return
    batch_file_path = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    batch_file_size = 0
    with open(batch_file_path, "a") as batch_file:
        with _lock_file(batch_file, exclusive=True):
            _terminate_partial_final_line(batch_file_path, batch_file)
            batch_file.write(line)
            batch_file.flush()
            batch_file_size = batch_file.tell()
    if platform.system() != "Windows":
        # A batch already written must not lose its shed to a failed
        # permission change: the record is on disk either way, and the
        # shed below is the only thing that keeps the file appendable.
        with contextlib.suppress(OSError):
            os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    _evict_batch_if_oversize(session_id, batch_file_size)


def read_batch(session_id: str) -> list[dict[str, Any]]:
    """Read all valid JSON entries from a session batch file.

    Args:
        session_id: Session whose batch is read.

    Returns:
        The decoded entries in file order; empty when there is no batch.
    """
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
    """Rewrite a session batch while holding an exclusive lock.

    Args:
        session_id: Session whose batch file is replaced.
        entries: The entries to write, in order.

    Returns:
        None.
    """
    batch_file_path = get_batch_file(session_id)
    with open(batch_file_path, "a+") as batch_file:
        with _lock_file(batch_file, exclusive=True):
            batch_file.seek(0)
            batch_file.truncate()
            for entry in entries:
                batch_file.write(json.dumps(entry, separators=(",", ":")) + "\n")
            # Flush inside the lock: truncate lands at once but the rewritten
            # lines sit in the buffer until close, which is after the lock is
            # released, so an append in that gap would be written over.
            batch_file.flush()
    if platform.system() != "Windows":
        # A batch already written must not lose its shed to a failed
        # permission change: the record is on disk either way, and the
        # shed below is the only thing that keeps the file appendable.
        with contextlib.suppress(OSError):
            os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    # Growing the file by rewriting it bypasses the append path's
    # size check, and on a build that refuses appends by size that
    # leaves the batch too large to accept a record and with nothing
    # left to shed it.
    _evict_batch_if_oversize(session_id, _batch_file_size(session_id))


def clear_batch(session_id: str) -> None:
    """Clear a session batch without deleting the coordination file.

    Args:
        session_id: Session whose batch contents are discarded.

    Returns:
        None.
    """
    rewrite_batch(session_id, [])
    # The delivered-prefix marker counts leading records of this batch, so it
    # goes with them. A count left standing over an emptied file describes
    # records that no longer exist, and the next sitting's records are then
    # measured against it and read as already delivered.
    with contextlib.suppress(OSError):
        os.unlink(_delivered_marker_path(session_id))


def _decode_batch_line(line: str) -> tuple[bool, Any]:
    """Decode one raw JSONL batch line.

    Returns ``(is_record, value)``. ``is_record`` is True only for a non-blank
    line that parses as JSON — exactly the lines ``read_batch`` returns and
    ``upload_batch`` sends — and False for a blank or corrupt line. ``value``
    holds the decoded object when ``is_record`` is True, else ``None``.

    Both ``read_batch`` and ``drop_leading_entries`` route through this so the
    records uploaded and the records drained can never diverge: corrupt lines
    are skipped identically on both sides.

    Args:
        line: One raw line read back from the batch file.

    Returns:
        ``(is_record, value)`` as described above.
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
    first is the one the session is filed under — so a session belonging to one
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

    Known limitation: a turn larger than one request cannot be sent
    turn-aligned, because no cut inside it lands on a terminator. The chunks
    after the first then open mid-turn, and the collector only builds events
    for a turn it has seen the start of, so those events are discarded even
    though the request succeeds. Closing this needs the collector to resolve a
    mid-turn request by its turn key the way it already resolves a turn-end; it
    cannot be fixed here alone.

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
# walk could exhaust the stack — which would abort the append's eviction and
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
    preserved and only text length changes — including dictionary keys, because
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
        # Capping keys can make two distinct entries collide, and the later
        # value would then silently replace the earlier. That happens two ways:
        # two long keys sharing a capped prefix, and a capped key landing on a
        # short key that is already its own capped form. Reserving the original
        # keys up front covers both — a capped key is never allowed onto any
        # original, and originals are unique by construction, so falling back
        # to one always resolves the clash. A longer envelope is recoverable;
        # a dropped entry is not.
        original_keys = set(value)
        capped_items: dict[Any, Any] = {}
        for key, item in value.items():
            capped_key = _cap_strings(key, character_limit, depth + 1)
            if capped_key != key and (
                capped_key in capped_items or capped_key in original_keys
            ):
                capped_key = key
            capped_items[capped_key] = _cap_strings(item, character_limit, depth + 1)
        return capped_items
    if isinstance(value, list):
        return [_cap_strings(item, character_limit, depth + 1) for item in value]
    return value


# Key left on a reduced envelope so the cut is visible in the collected data
# rather than looking like an envelope that arrived this sparse.
_REDUCED_MARKER_KEY = "bloomfilter_reduced"

# The envelope subtree every runtime's ingest config reads a turn's tokens from,
# and the key inside it that holds them. Reduction keeps these when it keeps
# nothing else: a turn without them is not merely thinner, it is a turn that
# reads as free.
_TOKEN_SOURCE_KEY = "transcript_summary"
_TOKEN_CALLS_KEY = "api_calls"


_PRICED_CALL_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_1h",
    # Emitted per call by the claude-code extractor alongside the 1h split, and
    # by codex from its rollout. Neither is read by the collector today, which is
    # why they were missed: the list was derived from what the server sums. They
    # are counts, so folding them as "newest call wins" would report one call's
    # value as the turn's total the moment anything starts reading them, and only
    # on the reduced path. Summed here regardless of who consumes them.
    "cache_creation_5m",
    "reasoning_output_tokens",
)

# A descriptive value is carried only while it stays small. This fold runs on
# the path that exists because an envelope was too large to send, so carrying an
# unbounded string would defeat the reduction it is part of.
_MAX_CARRIED_SCALAR_CHARS = 256


def _collapse_token_calls(api_calls: Any) -> list[dict[str, Any]]:
    """Aggregate a turn's API calls into one record per model.

    The collector reports every call a turn made, which on a long turn is
    thousands of records and larger than a whole request. The server sums them
    per model and reads a handful of scalars off the last one, so one summed
    record per model carries the same cost and the same identifiers while
    staying bounded by the number of models the turn used.

    Args:
        api_calls: The token source's call list, as the collector wrote it.

    Returns:
        One aggregated call per model, ordered so the last call's model is
        still last.
    """
    if not isinstance(api_calls, list):
        return []
    per_model: dict[str, dict[str, Any]] = {}
    last_index: dict[str, int] = {}
    for index, call in enumerate(api_calls):
        if not isinstance(call, dict):
            continue
        model_name = call.get("model") or ""
        bucket = per_model.setdefault(model_name, {"model": model_name})
        for field_name in _PRICED_CALL_FIELDS:
            value = call.get(field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            bucket[field_name] = bucket.get(field_name, 0) + int(value)
        for field_name, value in call.items():
            if field_name == "model" or field_name in _PRICED_CALL_FIELDS:
                continue
            # Everything else the call carries is described rather than summed,
            # so the newest value wins. Enumerating the known ones instead would
            # silently drop any the collector learns to send later — and an
            # installed build folds against whatever server it is pointed at,
            # which is not necessarily the one it was written against.
            if value is None or isinstance(value, (int, float, bool)):
                bucket[field_name] = value
            elif isinstance(value, str) and len(value) <= _MAX_CARRIED_SCALAR_CHARS:
                bucket[field_name] = value
        last_index[model_name] = index
    # Ordered by where each model was LAST seen, not where it was first.
    # The turn takes its model and identifiers from the newest call the server
    # is willing to price, and that walk skips placeholder and unnamed models —
    # so when the final call is one it skips, it continues back to the newest
    # real one. Preserving only the very last position would leave the models
    # behind it in first-seen order and the walk would stop on the wrong one.
    return [per_model[name] for name in sorted(per_model, key=last_index.__getitem__)]


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
        """Whether a value is small enough to carry into the reduced envelope.

        Args:
            value: A value read off the envelope being reduced.

        Returns:
            True for a short string or any scalar, False for anything larger.
        """
        if isinstance(value, str):
            return len(value) <= MIN_CAP_CHARS
        return isinstance(value, (int, float, bool)) or value is None

    reduced: dict[str, Any] = {}
    for key, value in entry.items():
        if keep(value):
            reduced[key] = value
        elif key == "payload" and isinstance(value, dict):
            reduced[key] = {
                inner_key: inner_value
                for inner_key, inner_value in value.items()
                if keep(inner_value)
            }
            reduced[key][_REDUCED_MARKER_KEY] = OVERSIZE_TEXT_MARKER
        elif key == _TOKEN_SOURCE_KEY and isinstance(value, dict):
            # The token counts stay, whatever else goes. They are what the cost
            # of the turn is computed from, and they are numbers, so keeping them
            # costs almost nothing however large the text was. Dropping them does
            # not make the turn look incomplete — it still finalises, still sets
            # its end time, and reads as a turn that genuinely cost nothing.
            preserved = {
                inner_key: inner_value
                for inner_key, inner_value in value.items()
                if inner_key == _TOKEN_CALLS_KEY
            }
            preserved[_REDUCED_MARKER_KEY] = OVERSIZE_TEXT_MARKER
            reduced[key] = _cap_strings(preserved, MIN_CAP_CHARS)
        else:
            reduced[key] = OVERSIZE_TEXT_MARKER
    if _entry_size(reduced) <= max_bytes:
        return reduced
    # Even the identifiers do not fit, which takes thousands of them. Keep what
    # names the envelope and nothing else, so its position is still held.
    minimal: dict[str, Any] = {
        "hook_event_name": entry.get("hook_event_name", ""),
        _REDUCED_MARKER_KEY: OVERSIZE_TEXT_MARKER,
    }
    # The token counts come with it. Dropping them here reintroduces exactly
    # what the branch above exists to prevent: the turn still finalises, still
    # sets its end time, and reads as a turn that genuinely cost nothing.
    # Collapsed per model so what is kept stays bounded however long the turn.
    token_source = entry.get(_TOKEN_SOURCE_KEY)
    if isinstance(token_source, dict):
        collapsed_calls = _collapse_token_calls(token_source.get(_TOKEN_CALLS_KEY))
        if collapsed_calls:
            minimal[_TOKEN_SOURCE_KEY] = {_TOKEN_CALLS_KEY: collapsed_calls}
    return minimal


def _shrink_entry(entry: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Return a copy of *entry* small enough to send, or the entry unchanged.

    Used for the envelopes that must not be dropped whatever their size. Halves
    the text budget until the envelope fits, so identifiers, event name and
    structure survive while the bulk — which is always long text — is cut.
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
    # encoded bytes, not characters — it opens with an ellipsis, which JSON
    # escapes to six bytes — and each string also carries its quotes and
    # separator. Under-counting either lands the first round a few bytes over
    # budget on exactly the envelopes this exists for.
    per_string_cost = _MARKER_ENCODED_BYTES + _STRING_PUNCTUATION_BYTES
    affordable = max_bytes // max(string_count, 1) - per_string_cost

    # Never above the longest string, or the round cuts nothing; never below the
    # floor, or the text left cannot show what was cut. A zero here means every
    # string sits below the depth the capping walk descends to, where whole
    # containers are replaced instead — so the floor is the right starting cap.
    character_limit = max(
        MIN_CAP_CHARS, min(affordable, longest_string or MIN_CAP_CHARS)
    )

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
            # every turn behind it. It is not kept either — no request holding
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
    eviction_limit: int | None = None,
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
    # Skipped only when there is nothing left to shed — never merely because the
    # protected envelopes alone exceed *max_bytes*. Reaching the budget was
    # never the contract: this function sheds down to the protected floor and
    # returns whatever that leaves. Bailing out early would keep the file above
    # MAX_BATCH_RETAINED_BYTES, which is the threshold `_append_is_refused`
    # gates on, so every later unprotected record would be refused for the rest
    # of the session while sheddable bytes sat in the file.
    protected_bytes = sum(
        _entry_size(entry)
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("hook_event_name") in PROTECTED_HOOK_EVENTS
    )
    if protected_bytes >= total_bytes:
        debug_log(
            "evict_low_value_entries: skipped, nothing unprotected to shed "
            f"protected_bytes={protected_bytes} total_bytes={total_bytes} "
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
    batch_file_handle: IO[str],
    max_bytes: int,
    session_id: str,
    delivered_count: int,
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
        session_id: Session the batch belongs to, for logging.
        delivered_count: Leading records the collector has already accepted.
            Passed in rather than read here: the caller holds an exclusive lock
            on this file, and on Windows that lock is mandatory, so re-opening
            the file from inside the locked region fails and the count would
            read as zero — bounding eviction to nothing.

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
        eviction_limit = _safe_eviction_limit(deliverable_entries, delivered_count)
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
    # lock is released — so an append that legitimately takes the lock in that
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
        current_session_id: Session being started, whose own batch is spared.
            Empty to sweep every stale batch.

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

    Network interactions are logged to <bloomfilter-config>/debug.log: the request
    URL + session_id + hook count + payload bytes, the response status +
    truncated body, and any HTTPError / URLError / unexpected exception.

    Args:
        api_url: Base URL of the Bloomfilter API, without the endpoint path.
        api_key: Value sent as the ``X-MCP-Token`` header.
        payload: Request body with ``session_id``, ``source``,
            ``plugin_version`` and a ``hooks`` list. Must be JSON-serializable.

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
    except (TypeError, ValueError) as exception:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} error={type(exception).__name__}: {exception}"
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
                # Also headers, not only the body: a request refused for
                # its size is never parsed, so the body's copy is exactly
                # what cannot be read when the sender matters most.
                # Version alone does not identify a build — several
                # share one — so the source travels with it.
                "X-Plugin-Version": PLUGIN_VERSION,
                "X-Plugin-Source": DEBUG_LOG_TAG,
            },
            method="POST",
        )
        # Unredirected: urlopen follows redirects with HTTPRedirectHandler,
        # which copies every ordinary header onto the new request — including
        # to a different host. Only Content-Length/Content-Type are dropped, so
        # an ordinary header here would hand the API key to whatever the
        # configured URL redirects to. add_unredirected_header keeps it on the
        # first request only.
        request.add_unredirected_header("X-MCP-Token", api_key)
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
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        The current UTC time as an ISO 8601 string.
    """
    return datetime.now(timezone.utc).isoformat()


def _cap_text(value: Any) -> Any:
    """Truncate an over-long string field; pass non-strings through unchanged.

    Subagent transcripts can carry very large tool outputs / responses. Cap
    them so a single batch upload stays bounded, mirroring the Claude Code
    plugin's behavior and the backend's field expectations.

    Args:
        value: Candidate field value; non-strings are returned untouched.

    Returns:
        The value, truncated with a marker when it exceeds the field cap.
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

    Args:
        conversation: Parsed child conversation, mutated in place.

    Returns:
        None.
    """
    for turn in conversation.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        if turn.get("user_prompt") is not None:
            turn["user_prompt"] = _cap_text(turn.get("user_prompt"))
        if turn.get("agent_response") is not None:
            turn["agent_response"] = _cap_text(turn.get("agent_response"))
        for tool_call in turn.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("tool_output") is not None:
                tool_call["tool_output"] = _cap_text(tool_call.get("tool_output"))


def extract_subagent_conversation(
    agent_transcript_path: str,
    expected_last_message: str | None = None,
    max_wait_s: float = 2.0,
    poll_s: float = 0.1,
) -> dict[str, Any] | None:
    """Parse a subagent's own Codex rollout into a normalized conversation.

    Codex fires ``SubagentStop`` before the subagent's final assistant message
    is guaranteed flushed to its rollout, so — when ``expected_last_message``
    is provided — this re-parses until the last turn's ``agent_response``
    matches it. On timeout it backfills the final response from the
    authoritative message so a partial capture can't survive.

    Args:
        agent_transcript_path: Path to the subagent thread's own rollout JSONL.
        expected_last_message: The authoritative
            ``payload.last_assistant_message``. When given, parsing repeats
            until the last turn's response matches it; when omitted, the first
            parse is accepted as-is.
        max_wait_s: Wall-clock ceiling on that re-parsing, in seconds.
        poll_s: Delay between re-parse attempts, in seconds.

    Returns:
        ``{"turns": [...]}`` — the backend's child-session shape — or None when
        the transcript path is empty or absent from disk. NOTE that a rollout
        which parses to no turns yields a TRUTHY ``{"turns": []}`` rather than
        None, so a caller gating on ``if conversation:`` will attach an empty
        child conversation. The cursor, claude-code and copilot collectors
        guard against that; this one does not.
    """
    if not agent_transcript_path or not os.path.exists(agent_transcript_path):
        return None

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
            last_response = (
                result.get("turns")[-1].get("agent_response") or ""
            ).strip()
        matched = bool(last_response) and last_response == expected_capped
        if matched or time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    # Never confirmed a complete match → the final response is missing or was
    # partially flushed. Replace it with the authoritative message.
    if result and expected and not matched and result.get("turns"):
        result.get("turns")[-1]["agent_response"] = _cap_text(expected_last_message)
    return result
