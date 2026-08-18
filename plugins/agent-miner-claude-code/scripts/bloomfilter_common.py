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
from typing import Optional, IO, Any, Iterator

# Platform-specific stdlib modules used by ``_lock_file`` below.
if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.3.0"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_TAG = "claude-code"  # disambiguates plugins sharing the same log dir

# Socket timeout for the batch upload, in seconds. Deliberately well under the
# per-hook timeout the runtime enforces (30s for Stop/SessionEnd in
# hooks/hooks.json) so that a stalled POST raises URLError *inside* this
# process — which debug_log records and which leaves the batch intact for the
# next attempt — instead of the runtime killing the process mid-request. When
# the two budgets were equal the runtime always won the race, so every stall
# became an unlogged SIGKILL.
UPLOAD_TIMEOUT_S = 15

# How long SessionEnd waits for an in-flight Stop upload to release the upload
# slot before giving up. Stop does not wait at all — its records ship with the
# next turn — but SessionEnd is a session's last chance to send anything, so
# skipping there would strand records with no later hook to pick them up.
# Budget check: this plus UPLOAD_TIMEOUT_S must stay inside the 30s hook
# timeout that hooks/hooks.json sets for SessionEnd (5 + 15 = 20s).
SESSION_END_SLOT_WAIT_S = 5

# Wall-clock ceiling for the 413 halving retries in one hook. Each POST can burn
# UPLOAD_TIMEOUT_S, so an unbounded retry loop would overrun the runtime's hook
# timeout and be SIGKILLed mid-request — the failure the timeout budget above
# exists to avoid. Exceeding it simply defers the rest to the next hook.
UPLOAD_RETRY_BUDGET_S = 8

# Wall-clock ceiling for the repeated drains the terminal hook performs. That
# hook is a session's last chance to ship anything, and one request may not
# carry a whole backlog, so it keeps going until the batch is empty. Bounded so
# a very large backlog cannot keep the process alive indefinitely.
SESSION_END_DRAIN_BUDGET_S = 60

# Poll interval while waiting for the upload slot.
SLOT_POLL_INTERVAL_S = 0.1

# Largest request body this client will attempt, in bytes. The collector API
# rejects anything above its own ceiling, which this client cannot raise, so
# staying below it is the client's responsibility. The margin left underneath
# absorbs JSON escaping and the envelope that wraps the entries measured here.
MAX_UPLOAD_BYTES = 2_000_000

# Whether this runtime re-sends the whole batch on every upload instead of
# removing what it has already delivered. It decides two things that must stay
# consistent: how large a batch may be retained, and what to do when the
# envelopes that can never be shed already exceed one request.
BATCH_IS_CUMULATIVE = False

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
PROTECTED_HOOK_EVENTS = frozenset(
    {"UserPromptSubmit", "Stop", "StopFailure", "SessionStart", "SessionEnd"}
)

# Evicted only after every unprotected envelope is gone: SubagentStop carries a
# child session's token totals, but can grow to a large share of a batch, so
# it cannot be protected outright.
DEFERRED_HOOK_EVENTS = frozenset({"SubagentStop"})

# Envelopes that close a turn. A request is cut here where possible so a turn is
# never split across two of them: a turn's records belong together, and sending
# them whole is what keeps each request self-describing rather than dependent on
# one that came before it.
TURN_TERMINAL_HOOK_EVENTS = frozenset({"Stop", "StopFailure", "SessionEnd"})

# Transcript tail read when extracting tokens. A much smaller window was used
# while the same slice also fed reasoning capture; reasoning is now scoped to the
# newest turn, so the window can widen to cover turns whose own Stop never fired
# without dragging the emitted payload along with it.
#
# Two independent budgets bound this, and the window is sized against the tighter
# reading of each. Cost scales with bytes read, not with the window setting, so a
# small transcript pays nothing for a large window:
#
#   - Time. The binding limit is the shortest hook timeout, which belongs to the
#     prompt-submit hook rather than the turn-stop hook -- prompt-submit is the
#     recovery path, so it is the one that must fit. Read-and-parse over a
#     multi-megabyte transcript lands in the tens of milliseconds, orders of
#     magnitude inside that budget.
#   - Payload. Only per-call token counts and turn keys are emitted, not the
#     transcript text, so what is read is far larger than what is sent and the
#     emitted payload stays well inside the request cap even at the widest window.
#
# Re-measure both if the emitted shape ever grows to include transcript content.
TRANSCRIPT_READ_BYTES = 8_000_000


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

    Always the bloomfilter config dir (~/.config/bloomfilter on macOS/Linux,
    %APPDATA%\\bloomfilter on Windows). Claude Code injects CLAUDE_PLUGIN_DATA
    pointing at a plugin-scoped cache dir, but we deliberately ignore it so
    debug.log lives next to the user's config.json and batches/ — one
    well-known place to look for diagnostics across all plugins.
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


def resolve_api_key():
    """Resolve the API key: env var > user config."""
    key = os.environ.get("BLOOMFILTER_API_KEY", "")
    if key:
        return _sanitize_api_key(key)

    user_config = os.path.join(get_config_dir(), "config.json")
    return _sanitize_api_key(read_json_config(user_config, "api_key"))


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


def _resolve_git_executable() -> str:
    """Return an absolute path to git, or '' when none can be trusted.

    Never returns a bare name or a relative path. On Windows a process started
    without an explicit executable path searches the current directory before
    PATH, so a repository that ships its own git-named binary would run instead
    of the real one purely because the editor opened that folder.

    Returns:
        Absolute path to a git executable, or '' if none was found.
    """
    candidate = shutil.which("git") or ""
    # shutil.which can itself return a cwd-relative hit on Windows; an absolute
    # path is the only form that cannot be redirected by the opened folder.
    if candidate and os.path.isabs(candidate) and os.path.isfile(candidate):
        return candidate
    return ""


def get_git_branch(project_dir):
    """Return the current git branch, or '' on failure."""
    git_executable = _resolve_git_executable()
    if not git_executable:
        return ""
    try:
        result = subprocess.run(
            [git_executable, "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Batch file locking
# ---------------------------------------------------------------------------
#
# Every batch mutation below runs under an advisory cross-process lock. Hooks
# are separate short-lived processes and several can overlap — a Stop upload
# draining the batch while a PostToolUse from the next turn appends to it — so
# an unsynchronized read-modify-write would drop entries.


if platform.system() != "Windows":

    @contextlib.contextmanager
    def _lock_file(file_handle: IO, exclusive: bool = True) -> Iterator[None]:
        """Hold an advisory ``flock`` on an open file for the block's duration.

        Args:
            file_handle: An open file object. Its descriptor is locked and its
                file position is left untouched.
            exclusive: True for a write lock (``LOCK_EX``), False for a shared
                read lock (``LOCK_SH``).

        Yields:
            None. The lock is held for the body of the ``with`` statement and
            released on exit, including when the body raises.
        """
        lock_operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(file_handle, lock_operation)
        try:
            yield
        finally:
            fcntl.flock(file_handle, fcntl.LOCK_UN)

    def _try_lock_exclusive(file_handle: IO) -> None:
        """Take an exclusive lock without waiting for it.

        Args:
            file_handle: An open file object whose descriptor is locked.

        Raises:
            OSError: If another process already holds the lock.
        """
        fcntl.flock(file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(file_handle: IO) -> None:
        """Release a lock previously taken by :func:`_try_lock_exclusive`.

        Args:
            file_handle: The file object that was locked. Failures are ignored,
                since the process is exiting the guarded section either way.
        """
        try:
            fcntl.flock(file_handle, fcntl.LOCK_UN)
        except OSError:
            pass

else:

    @contextlib.contextmanager
    def _lock_file(file_handle: IO, exclusive: bool = True) -> Iterator[None]:
        """Hold a cross-process byte-range lock via ``msvcrt.locking``.

        Locks a single byte at offset 0 as a coordination token. ``LK_LOCK``
        retries once a second up to ten times before raising; if it does raise,
        the body still runs unsynchronized, because degrading is preferable to
        crashing the host's hook.

        The file position is saved and restored around the lock so that the
        seek to offset 0 does not disturb append-mode writes.

        Args:
            file_handle: An open file object whose descriptor is locked.
            exclusive: Accepted for parity with the POSIX implementation and
                ignored — msvcrt offers exclusive locks only.

        Yields:
            None. The lock is released on exit when it was acquired at all.
        """
        try:
            file_handle.flush()
        except (OSError, ValueError):
            pass
        try:
            saved_position = file_handle.tell()
        except (OSError, ValueError):
            saved_position = None

        def restore_saved_position() -> None:
            if saved_position is not None:
                try:
                    file_handle.seek(saved_position)
                except (OSError, ValueError):
                    pass

        try:
            file_handle.seek(0)
            msvcrt.locking(file_handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as lock_error:
            print(
                f"[bloomfilter] Could not acquire batch file lock "
                f"({lock_error}); proceeding unsynchronized.",
                file=sys.stderr,
            )
            restore_saved_position()
            yield
            return

        try:
            restore_saved_position()
            yield
        finally:
            try:
                file_handle.seek(0)
                msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            restore_saved_position()

    def _try_lock_exclusive(file_handle: IO) -> None:
        """Take an exclusive lock without waiting for it.

        Uses ``LK_NBLCK``, which fails immediately rather than ``LK_LOCK``'s
        retry-every-second-for-ten-seconds. That immediacy is what makes this
        usable as an "is another upload already running?" test.

        Args:
            file_handle: An open file object whose descriptor is locked.

        Raises:
            OSError: If another process already holds the lock.
        """
        file_handle.seek(0)
        msvcrt.locking(file_handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(file_handle: IO) -> None:
        """Release a lock previously taken by :func:`_try_lock_exclusive`.

        Args:
            file_handle: The file object that was locked. Failures are ignored,
                since the process is exiting the guarded section either way.
        """
        try:
            file_handle.seek(0)
            msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Batch file helpers
# ---------------------------------------------------------------------------


def get_batch_dir() -> str:
    """Return the directory holding per-session batch files, creating it.

    Returns:
        Absolute path to ``<config-dir>/batches``.
    """
    batch_dir_path = os.path.join(get_config_dir(), "batches")
    # Refuse a symlinked batch directory. The config root is taken from the
    # environment, so anything able to set that for the editor's child processes
    # could point this at a directory of its choosing -- and the age sweep below
    # deletes files there. A real directory is the only thing safe to sweep.
    if os.path.islink(batch_dir_path):
        debug_log(f"get_batch_dir: refusing symlinked batch dir {batch_dir_path}")
        raise RuntimeError("batch directory must not be a symlink")
    secure_makedirs(batch_dir_path)
    return batch_dir_path


def get_batch_file(session_id: str) -> str:
    """Return the path of the JSONL batch file for one session.

    Args:
        session_id: Session identifier taken from the hook payload. It becomes
            the file stem verbatim, so it must be a bare filename component.

    Returns:
        Absolute path to ``<batch-dir>/<session_id>.jsonl``.

    Raises:
        ValueError: If *session_id* is empty, contains a path separator, or
            contains a parent-directory reference — any of which would let a
            crafted payload write outside the batch directory.
    """
    sanitized_session_id = os.path.basename(session_id)
    if (
        not sanitized_session_id
        or sanitized_session_id != session_id
        or ".." in session_id
    ):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{sanitized_session_id}.jsonl")


@contextlib.contextmanager
def upload_slot(session_id: str, wait_seconds: float = 0.0) -> Iterator[bool]:
    """Claim the exclusive right to upload one session's batch.

    The snapshot-upload-drain sequence is deliberately NOT atomic: the batch
    lock is released for the duration of the POST so that tool hooks can keep
    appending instead of blocking on the network. That leaves one hazard — two
    overlapping upload hooks (a Stop whose POST is still running when
    SessionEnd fires) would each snapshot the same N records, each upload them,
    and then each drain N, the second drain deleting N records that were never
    sent. This guard makes uploads single-flight per session so that interleave
    cannot occur; the loser skips, and its entries go out with the next batch.

    The lock lives in a sidecar file rather than the batch file itself, because
    the batch lock must stay free while the POST is in flight.

    Args:
        session_id: Session whose upload slot is being claimed.
        wait_seconds: How long to keep retrying before giving up. Stop passes 0
            and skips at once, because another turn will ship its records. There
            is no turn after SessionEnd, so it passes a budget instead — records
            skipped there would sit in the batch file with nothing left to
            upload them.

    Yields:
        True if this process claimed the slot and may upload, False if another
        upload is already in flight and this one should skip.
    """
    upload_lock_path = get_batch_file(session_id) + ".upload"
    with open(upload_lock_path, "a+") as lock_file_handle:
        wait_deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                _try_lock_exclusive(lock_file_handle)
                break
            except OSError:
                if time.monotonic() >= wait_deadline:
                    yield False
                    return
                time.sleep(SLOT_POLL_INTERVAL_S)
        try:
            yield True
        finally:
            _unlock(lock_file_handle)


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
    thousands of times per session, and a read-modify-write on each one would be
    orders of magnitude dearer than the append it guards.

    Eviction must not run while an upload is in flight. The uploader snapshots
    N records, POSTs, then drains N *by count* — so anything that removes
    records from the head during the POST redirects that drain onto records
    that were never sent, destroying them. Taking the upload slot makes the
    two mutually exclusive; failing to take it means an upload is running, and
    a later append will evict instead. Never waits: this is a tool hook.

    Args:
        session_id: Session whose batch file may need shedding.
        batch_file_size: Size of that file in bytes, as of the append.

    Returns:
        None.
    """
    if batch_file_size <= MAX_BATCH_RETAINED_BYTES:
        return
    with upload_slot(session_id, wait_seconds=0.0) as has_upload_slot:
        if not has_upload_slot:
            return
        evicted_count = 0
        with open(get_batch_file(session_id), "a+") as batch_file_handle:
            with _lock_file(batch_file_handle, exclusive=True):
                evicted_count = _evict_locked_batch(
                    batch_file_handle, BATCH_EVICTION_TARGET_BYTES, session_id
                )
    if evicted_count:
        debug_log(
            f"_evict_batch_if_oversize: evicted={evicted_count} session_id={session_id}"
        )


def append_to_batch(session_id: str, entry: dict[str, Any]) -> None:
    """Append one envelope to a session's batch file as a JSON line.

    Once the file exceeds :data:`MAX_BATCH_RETAINED_BYTES` the low-value
    envelopes are shed (see :func:`evict_low_value_entries`) so a session that
    outruns its own upload rate cannot grow the file without bound. The size
    check is a single stat in the common case — this runs on every tool hook,
    thousands of times per session, so the read-modify-write must stay behind a
    threshold rather than happening on each append.

    Args:
        session_id: Session the entry belongs to.
        entry: The hook envelope to persist. Must be JSON-serializable.
    """
    if _append_is_refused(session_id, entry):
        return
    batch_file_path = get_batch_file(session_id)
    serialized_entry = json.dumps(entry, separators=(",", ":")) + "\n"
    batch_file_size = 0
    with open(batch_file_path, "a") as batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=True):
            batch_file_handle.write(serialized_entry)
            batch_file_handle.flush()
            batch_file_size = batch_file_handle.tell()
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    _evict_batch_if_oversize(session_id, batch_file_size)


def _decode_batch_line(raw_line: str) -> tuple[bool, Any]:
    """Decode one raw JSONL line from a batch file.

    Both :func:`read_batch` and :func:`drop_leading_entries` route through this
    so that the records uploaded and the records drained can never diverge:
    corrupt lines are skipped identically on both sides.

    Args:
        raw_line: A single line read from a batch file, newline included.

    Returns:
        A ``(is_record, decoded_entry)`` pair. ``is_record`` is True only for a
        non-blank line that parses as JSON — exactly the lines ``read_batch``
        returns and ``upload_batch`` sends. ``decoded_entry`` holds the decoded
        object when ``is_record`` is True and None otherwise.
    """
    stripped_line = raw_line.strip()
    if not stripped_line:
        return False, None
    try:
        return True, json.loads(stripped_line)
    except json.JSONDecodeError:
        return False, None


def read_batch(session_id: str) -> list[dict[str, Any]]:
    """Read every valid entry from a session's batch file without removing any.

    The file is opened read/write when possible purely so that the lock can be
    taken on Windows: ``msvcrt.locking`` has no shared mode, so even this read
    takes an exclusive byte-range lock, and read-only descriptors are widely
    reported to be rejected by it. Microsoft's own ``_locking`` example locks an
    ``_O_RDONLY`` descriptor, so plain ``"r"`` is expected to work — the
    fallback costs nothing and keeps the batch readable even when the file is
    read-only, which ``"r+"`` alone would turn into a hard failure.

    Args:
        session_id: Session whose batch is read.

    Returns:
        The decoded entries in file order. Empty when the batch file is missing
        or holds no valid records; blank and corrupt lines are skipped.
    """
    batch_file_path = get_batch_file(session_id)
    if not os.path.isfile(batch_file_path):
        return []
    try:
        batch_file_handle = open(batch_file_path, "r+")
    except OSError:
        batch_file_handle = open(batch_file_path, "r")
    with batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=False):
            raw_lines = batch_file_handle.readlines()
    entries = []
    for raw_line in raw_lines:
        is_record, decoded_entry = _decode_batch_line(raw_line)
        if is_record:
            entries.append(decoded_entry)
    return entries


def rewrite_batch(session_id: str, entries: list[dict[str, Any]]) -> None:
    """Replace a session's batch file contents with *entries*.

    Opened with ``a+`` so the file is not truncated until *after* the exclusive
    lock is acquired. Concurrent :func:`append_to_batch` calls block on the same
    lock and never lose a line.

    Args:
        session_id: Session whose batch is rewritten.
        entries: Entries to write, in order. An empty list empties the file.
    """
    batch_file_path = get_batch_file(session_id)
    with open(batch_file_path, "a+") as batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=True):
            batch_file_handle.seek(0)
            batch_file_handle.truncate()
            for entry in entries:
                batch_file_handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def cleanup_session_batch(session_id: str) -> None:
    """Delete a session's drained batch file and its upload lock sidecar.

    :func:`drop_leading_entries` leaves a zero-byte file behind once it drains
    the last records, and :func:`upload_slot` leaves a zero-byte lock file, so
    without this every session would leak two directory entries into
    ``batches/`` forever.

    Only safe to call at SessionEnd, which is terminal: no tool hook can still
    be appending and no further upload will start. Emptiness is checked while
    holding the exclusive lock and the batch file is unlinked only when it holds
    no records, so a record can never be deleted unsent. The unlink happens
    after the handle closes, because Windows cannot unlink an open file.

    Nothing is removed unless the upload slot can be acquired first. If an
    upload is still in flight, the lock file is the token protecting it:
    unlinking it would let the next process create a fresh file at the same path
    and take an uncontended lock, defeating the mutual exclusion. Leaving both
    files in place is the safe outcome — the batch still holds unsent records in
    that case anyway.

    Args:
        session_id: Session whose files are removed. A batch still holding
            records — an upload that failed, say — is deliberately left alone.
    """
    batch_file_path = get_batch_file(session_id)
    upload_lock_path = batch_file_path + ".upload"

    with open(upload_lock_path, "a+") as lock_file_handle:
        try:
            _try_lock_exclusive(lock_file_handle)
        except OSError:
            debug_log(
                f"cleanup skipped: session_id={session_id} "
                "reason=upload-still-in-flight"
            )
            return
        try:
            if os.path.isfile(batch_file_path):
                with open(batch_file_path, "a+") as batch_file_handle:
                    with _lock_file(batch_file_handle, exclusive=True):
                        batch_file_handle.seek(0)
                        has_no_records = not any(
                            _decode_batch_line(raw_line)[0]
                            for raw_line in batch_file_handle
                        )
                if has_no_records:
                    try:
                        os.remove(batch_file_path)
                    except OSError:
                        pass
                else:
                    debug_log(
                        f"cleanup: session_id={session_id} batch retained "
                        "reason=unsent-records-remain"
                    )
        finally:
            _unlock(lock_file_handle)

    # Unlink after the handle closes (Windows cannot unlink an open file) and
    # after the lock is released, having confirmed no other uploader held it.
    try:
        os.remove(upload_lock_path)
    except OSError:
        pass


def drop_leading_entries(session_id: str, record_count: int) -> None:
    """Remove the first *record_count* records from a session's batch file.

    Called after a successful upload to delete exactly the records that were
    sent, while preserving any that :func:`append_to_batch` added concurrently —
    those land after the uploaded snapshot, so they survive as the trailing
    lines here. This is the safe alternative to truncating the whole file, which
    would discard those concurrent appends unsent.

    The read-modify-write happens under a single exclusive lock (``a+`` so the
    file is not truncated until the lock is held), so a concurrent append either
    completes before this runs, and is preserved, or blocks until after.

    Only the valid JSON records :func:`read_batch` would return are counted, so
    corrupt or blank lines in the leading region are discarded without consuming
    the count. Counting them instead would exhaust the budget early and leave an
    already-uploaded record behind to be re-sent in the next batch.

    Args:
        session_id: Session whose batch is drained.
        record_count: How many leading records to remove, normally
            ``len(entries)`` from the snapshot that was just uploaded. Values of
            zero or less are a no-op.
    """
    if record_count <= 0:
        return
    batch_file_path = get_batch_file(session_id)
    if not os.path.isfile(batch_file_path):
        return
    with open(batch_file_path, "a+") as batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=True):
            batch_file_handle.seek(0)
            raw_lines = batch_file_handle.readlines()
            retained_lines = []
            dropped_record_count = 0
            for raw_line in raw_lines:
                if dropped_record_count < record_count:
                    is_record, _ = _decode_batch_line(raw_line)
                    if is_record:
                        dropped_record_count += 1
                    continue
                retained_lines.append(raw_line)
            batch_file_handle.seek(0)
            batch_file_handle.truncate()
            batch_file_handle.writelines(retained_lines)
            # Flush inside the lock: truncate lands at once but the kept lines
            # sit in the buffer until close, which is after the lock is released,
            # so an append in that gap would be written over.
            batch_file_handle.flush()
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


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


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


def upload_batch(api_url: str, api_key: str, payload: dict[str, Any]) -> str:
    """POST a batch of hook envelopes to the Bloomfilter API.

    The URL scheme is validated up front — only http and https are accepted.
    Every network interaction is recorded in ``<bloomfilter-config>/debug.log``:
    the request URL, session id, record count and payload size; the response
    status and a truncated body; and any HTTPError, URLError or unexpected
    exception.

    The socket timeout is :data:`UPLOAD_TIMEOUT_S`, deliberately shorter than
    the runtime's per-hook budget so a stalled POST fails here — and is logged —
    rather than being killed mid-request from outside.

    Args:
        api_url: Base URL of the Bloomfilter API, without the endpoint path.
        api_key: Value sent as the ``X-MCP-Token`` header.
        payload: Request body with ``session_id``, ``source``,
            ``plugin_version`` and a ``hooks`` list. Must be JSON-serializable.

    Returns:
        :data:`UPLOAD_OK` when the server answered 2xx, meaning the records are
        safe to drain. :data:`UPLOAD_TOO_LARGE` when it answered 413, meaning
        the caller must send fewer records rather than retry this body — an
        identical oversize request can never succeed, so collapsing 413 into the
        generic failure is what makes an oversize batch permanent.
        :data:`UPLOAD_FAILED` for an invalid URL, an unserializable payload, a
        transport error, or any other non-2xx status; the caller keeps the
        records and retries later.
    """
    parsed_api_url = urllib.parse.urlparse(api_url or "")
    if parsed_api_url.scheme not in ("http", "https") or not parsed_api_url.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return UPLOAD_FAILED

    # Accessing .port raises for a malformed port (non-numeric or out of range),
    # so validate it here rather than letting it throw further down.
    try:
        parsed_api_url.port  # noqa: B018 — evaluated for its validation side effect
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
    if parsed_api_url.scheme == "http" and parsed_api_url.hostname not in {
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

    endpoint_url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    record_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0

    try:
        # Compact separators, matching how entries are measured on the way in
        # and written to the batch file. Default separators add ", " and ": ",
        # inflating the body by up to 1.5x for structured payloads — enough for a
        # batch that measured under the client cap to still be rejected as too
        # large, which is the failure this cap exists to prevent.
        encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as serialization_error:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} "
            f"error={type(serialization_error).__name__}: {serialization_error}"
        )
        return UPLOAD_FAILED

    debug_log(
        f"upload_batch: sending POST {endpoint_url} session_id={session_id} "
        f"hooks={record_count} bytes={len(encoded_payload)}"
    )

    try:
        request = urllib.request.Request(
            endpoint_url,
            data=encoded_payload,
            headers={
                "Content-Type": "application/json",
                "X-MCP-Token": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_S) as response:
            status_code = response.getcode()
            response_body = response.read().decode("utf-8", errors="replace")
        debug_log(
            f"upload_batch: response status={status_code} session_id={session_id} "
            f"body={response_body[:500]!r}"
        )
        if not 200 <= status_code < 300:
            print(
                f"[bloomfilter] Upload response status: {status_code}",
                file=sys.stderr,
            )
        return UPLOAD_OK if 200 <= status_code < 300 else UPLOAD_FAILED
    except urllib.error.HTTPError as http_error:
        try:
            error_body = http_error.read().decode("utf-8", errors="replace").strip()
        except Exception:
            error_body = ""
        failure_reason = getattr(http_error, "reason", "")
        debug_log(
            f"upload_batch: HTTPError status={http_error.code} "
            f"reason={failure_reason!r} session_id={session_id} "
            f"body={error_body[:500]!r}"
        )
        # 413 is expected control flow now, not an error to report: the caller
        # answers it by sending a smaller prefix. Printing it would put two lines
        # of HTTP error text in the user's terminal on a path that recovers by
        # itself. It stays in debug.log above.
        if http_error.code == 413:
            return UPLOAD_TOO_LARGE
        stderr_message = f"[bloomfilter] Upload failed with HTTP {http_error.code}"
        if failure_reason:
            stderr_message += f" {failure_reason}"
        print(stderr_message, file=sys.stderr)
        if error_body:
            print(
                f"[bloomfilter] Upload response body: {error_body[:500]}",
                file=sys.stderr,
            )
        return UPLOAD_FAILED
    except urllib.error.URLError as url_error:
        debug_log(
            f"upload_batch: URLError session_id={session_id} "
            f"reason={url_error.reason!r}"
        )
        print(f"[bloomfilter] Upload failed: {url_error.reason}", file=sys.stderr)
        return UPLOAD_FAILED
    except Exception as unexpected_error:
        debug_log(
            f"upload_batch: error session_id={session_id} "
            f"type={type(unexpected_error).__name__} message={unexpected_error!s}"
        )
        print(f"[bloomfilter] Upload failed: {unexpected_error}", file=sys.stderr)
        return UPLOAD_FAILED


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utcnow_iso():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Token extraction (kept client-side — transcript is a local file)
# ---------------------------------------------------------------------------


def _extract_api_calls(scoped_entries):
    """Build the deduplicated api_call list for one turn's entries.

    Args:
        scoped_entries: Transcript entries belonging to a single turn, in order.

    Returns:
        list[dict]: One entry per distinct API call, carrying token counts, the
        cache-write TTL split, model, response id and stop reason.
    """
    usage_entries = [
        e
        for e in scoped_entries
        if (
            e.get("type") == "assistant"
            or e.get("message", {}).get("role") == "assistant"
        )
        and e.get("message", {}).get("usage")
    ]
    if not usage_entries:
        return []

    # Streaming writes one response across several lines sharing a message id,
    # so collapse on it. When that id is empty, fall back to requestId — keying
    # every id-less call on "" collapsed distinct calls into one bucket and
    # discarded all but the last. When both are absent the entry keeps its own
    # slot rather than joining a shared one.
    deduplicated = {}
    for position, entry in enumerate(usage_entries):
        message = entry.get("message", {})
        dedup_key = message.get("id", "") or entry.get("requestId", "")
        deduplicated[dedup_key or ("__no_key__", position)] = entry

    api_calls = []
    for entry in deduplicated.values():
        message = entry.get("message", {})
        usage = message.get("usage", {})
        # The split is nested under usage.cache_creation. Absent means "no 1h
        # tokens reported", never "unknown": reporting zero rather than omitting
        # the field keeps the shape stable, so a payload without the split is
        # indistinguishable from one that genuinely had none.
        cache_creation = usage.get("cache_creation") or {}
        api_call = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_creation_5m": cache_creation.get("ephemeral_5m_input_tokens", 0),
            "cache_creation_1h": cache_creation.get("ephemeral_1h_input_tokens", 0),
            "model": message.get("model", ""),
            "response_id": message.get("id", ""),
            "stop_reason": message.get("stop_reason", ""),
        }
        speed = usage.get("speed")
        if speed:
            api_call["speed"] = speed
        api_calls.append(api_call)
    return api_calls


# The runtime records the conversation's display name in the transcript rather
# than in any hook payload, under two entry kinds: one written when the user
# names the conversation themselves, and one holding a name generated from the
# opening prompt. A user-chosen name always wins, and the newest of each kind
# wins, because a conversation can be renamed at any point in its life.
#
# Both kinds are re-emitted as the conversation grows, so the tail already in
# hand carries them and no extra read is needed.
TITLE_ENTRY_FIELDS = (("custom-title", "customTitle"), ("ai-title", "aiTitle"))


def _extract_session_title(entries):
    """Return the conversation's display name from transcript entries.

    Args:
        entries: Parsed transcript entries, in file order.

    Returns:
        The name to report, or "" when the transcript carries none.
    """
    found = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        for candidate_type, field in TITLE_ENTRY_FIELDS:
            if entry_type != candidate_type:
                continue
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                found[candidate_type] = value.strip()
    for candidate_type, _ in TITLE_ENTRY_FIELDS:
        if found.get(candidate_type):
            return found[candidate_type]
    return ""


def extract_transcript_summary(transcript_path):
    """Parse transcript JSONL and return a condensed token summary.

    Returns a dict with an ``api_calls`` list, or None on failure.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None

    try:
        file_size = os.path.getsize(transcript_path)
        # Anchored at EOF and walking back, never anchored at a stored offset
        # reading forward: a turn whose span exceeds the window would then push
        # the newest turn permanently out of view, and the backlog would grow
        # faster than it drained.
        read_start = max(0, file_size - TRANSCRIPT_READ_BYTES)
        with open(transcript_path, "rb") as transcript_file:
            transcript_file.seek(read_start)
            raw = transcript_file.read()
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

        # Every real user prompt starts a turn. The scan exists to find where
        # the newest one begins; a tool result is not a prompt, so it does not
        # open a turn.
        turn_start_indexes = []
        for i, entry in enumerate(entries):
            if entry.get("type") != "user":
                continue
            if entry.get("toolUseResult"):
                continue
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list) and all(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
            ):
                continue
            turn_start_indexes.append(i)

        last_user_idx = turn_start_indexes[-1] if turn_start_indexes else -1

        # Entries of the newest turn. When the window holds no prompt at all the
        # turn began before it: the calls are still reported on the legacy field
        # so nothing is lost, but they are NOT grouped, because attributing them
        # to a turn we cannot name is what made one message id ship under two
        # different prompt ids across consecutive hooks.
        turn_entries = entries[last_user_idx + 1 :] if last_user_idx >= 0 else entries

        api_calls = _extract_api_calls(turn_entries)
        session_title = _extract_session_title(entries)
        # The newest turn can legitimately have no calls yet — a prompt that has
        # not been answered, or a tail of non-assistant entries. A conversation
        # name on its own is still worth reporting, so bail only when there is
        # nothing at all to send.
        if not api_calls and not session_title:
            return None

        # Extract thinking/reasoning in transcript order so the backend can build
        # THINKING events. Reasoning lives ONLY in the transcript — it is never in
        # a hook payload (last_assistant_message is final text only). Claude Code
        # writes each content block on its own assistant line and SHARES one
        # message id across a response's thinking/text/tool_use lines, so we walk
        # the raw turn entries here: deduping by id (as the token logic above
        # must, to avoid triple-counting usage) would drop the thinking and text
        # lines and keep only the last block. position = number of tool_use blocks
        # preceding the thought, matching the backend's interleave scheme.
        thinking = []
        tool_use_count = 0
        # Seed with the turn's user-prompt timestamp (it sits just before
        # turn_entries) so the FIRST thought's duration spans from the prompt.
        prev_ts = (
            entries[last_user_idx].get("timestamp") if last_user_idx >= 0 else None
        )
        for entry in turn_entries:
            ts = entry.get("timestamp")
            is_assistant = (
                entry.get("type") == "assistant"
                or entry.get("message", {}).get("role") == "assistant"
            )
            content = entry.get("message", {}).get("content") if is_assistant else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "thinking":
                        text = block.get("thinking", "")
                        if text:
                            thinking.append(
                                _thinking_entry(
                                    tool_use_count,
                                    prev_ts,
                                    ts,
                                    content=_cap_text(text),
                                )
                            )
                        elif block.get("signature"):
                            # Opus extended-thinking text is encrypted — Claude
                            # Code persists only the signature, never plaintext.
                            # Emit an encrypted marker so the timeline still shows
                            # the model reasoned (mirrors the Codex case).
                            thinking.append(
                                _thinking_entry(
                                    tool_use_count, prev_ts, ts, encrypted=True
                                )
                            )
                    elif block_type == "redacted_thinking":
                        thinking.append(
                            _thinking_entry(tool_use_count, prev_ts, ts, encrypted=True)
                        )
                    elif block_type == "tool_use":
                        tool_use_count += 1
            # Track the previous entry's timestamp (from ANY entry, incl. tool
            # results) so a thinking block's duration spans from the real prior
            # event, not just the prior assistant line.
            if ts:
                prev_ts = ts

        # Everything here is the newest turn only. A turn whose own end hook
        # never fired is recovered by the next prompt instead, which reports the
        # calls it can still see; shipping a per-turn breakdown as well was
        # measured at ~93% of this payload and had no consumer, and payload size
        # is the binding constraint on delivery. thinking in particular must stay
        # scoped to one turn: it is reasoning plaintext, capped per block but not
        # in count, and every block is filed against the single turn handed over.
        summary = {"api_calls": api_calls}
        if thinking:
            summary["thinking"] = thinking
        if session_title:
            summary["session_title"] = session_title
        return summary

    except Exception:
        return None


# Cap a single tool_output/text payload so a subagent that read large files
# doesn't bloat the batch upload. Generous enough to keep summaries intact.
_SUBAGENT_FIELD_CAP = 10_000


def _cap_text(value: Any) -> Any:
    """Truncate a string to the subagent field cap; return it unchanged otherwise.

    Args:
        value: The string to cap.

    Returns:
        str: The string, truncated to _SUBAGENT_FIELD_CAP chars if longer.
    """
    # Anything that is not text is returned as it came: a transcript block
    # whose field is a container must not raise out of extraction.
    if not isinstance(value, str):
        return value
    if len(value) > _SUBAGENT_FIELD_CAP:
        return value[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
    return value


def _parse_iso_ts(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp (trailing Z tolerated) into a datetime.

    Args:
        value: An ISO 8601 timestamp string, or anything else.

    Returns:
        datetime | None: The parsed datetime, or None if unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _duration_ms(start_ts: str | None, end_ts: str | None) -> int | None:
    """Best-effort elapsed milliseconds between two ISO timestamps.

    Args:
        start_ts: The earlier ISO timestamp string.
        end_ts: The later ISO timestamp string.

    Returns:
        int | None: Non-negative milliseconds, or None if either timestamp is
            missing/unparseable or the span is negative (clock skew).
    """
    start = _parse_iso_ts(start_ts)
    end = _parse_iso_ts(end_ts)
    if not start or not end:
        return None
    ms = int((end - start).total_seconds() * 1000)
    return ms if ms >= 0 else None


def _thinking_entry(
    position: int,
    prev_ts: str | None,
    ts: str | None,
    content: str | None = None,
    encrypted: bool = False,
) -> dict:
    """Build one thinking entry for the batch payload.

    ``duration_ms`` is best-effort: the elapsed time from the previous transcript
    entry to this thinking block's own timestamp. Thinking is the first block a
    response emits, so this approximates request latency + reasoning generation —
    the transcript exposes no finer signal (and no reasoning-token count).

    Args:
        position: Number of tool_use blocks preceding this thought in the turn.
        prev_ts: Timestamp of the previous transcript entry (duration start).
        ts: This thinking entry's timestamp (duration end).
        content: Readable reasoning text, or None when encrypted/unavailable.
        encrypted: True when only an encrypted signature exists (no text).

    Returns:
        dict: ``{position, [content], [encrypted], [started_at], [duration_ms]}``.
    """
    entry = {"position": position}
    if content is not None:
        entry["content"] = content
    if encrypted:
        entry["encrypted"] = True
    if ts:
        # The block's own timestamp — lets the backend order thinking
        # chronologically among the turn's tool calls (true interleave).
        entry["started_at"] = ts
    duration = _duration_ms(prev_ts, ts)
    if duration:  # omit missing (None) and meaningless zero-length spans
        entry["duration_ms"] = duration
    return entry


def extract_subagent_conversation(
    agent_transcript_path: str,
    expected_last_message: str | None = None,
    max_wait_s: float = 2.0,
    poll_s: float = 0.1,
) -> dict | None:
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


def _parse_subagent_transcript(agent_transcript_path: str) -> dict | None:
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
        with open(agent_transcript_path, "rb") as transcript_file:
            raw = transcript_file.read()
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
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
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
                totals["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                totals["cache_creation_tokens"] += usage.get(
                    "cache_creation_input_tokens", 0
                )
            turn.update(totals)
            turn["tool_calls"] = list(turn.pop("_tool_calls_by_id", {}).values())
            turn["thinking"] = turn.pop("_thinking", [])
            return turn

        prev_ts = None
        for entry in entries:
            entry_type = entry.get("type")
            msg = entry.get("message", {})
            ts = entry.get("timestamp")
            # Previous entry's timestamp (duration start for a thinking block),
            # captured before advancing prev_ts to this entry.
            entry_prev_ts = prev_ts
            if ts:
                prev_ts = ts

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
                    "_thinking": [],
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
                    "_thinking": [],
                }

            if ts:
                current["ended_at"] = ts

            is_assistant = entry_type == "assistant" or msg.get("role") == "assistant"
            if is_assistant:
                if msg.get("usage"):
                    message_id = msg.get("id", "")
                    current["_usage_by_id"][message_id] = msg["usage"]
                    if msg.get("model"):
                        current["model"] = msg["model"]
                    if message_id:
                        current["response_id"] = message_id
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text" and block.get("text"):
                            current["agent_response"] = _cap_text(block["text"])
                        elif block_type == "thinking":
                            # position = tool calls seen so far, so the backend
                            # renders this thought before that tool (trailing
                            # thinking when it equals the final tool count).
                            if block.get("thinking"):
                                current["_thinking"].append(
                                    _thinking_entry(
                                        len(current["_tool_calls_by_id"]),
                                        entry_prev_ts,
                                        ts,
                                        content=_cap_text(block["thinking"]),
                                    )
                                )
                            elif block.get("signature"):
                                # Encrypted thinking — only a signature persists.
                                current["_thinking"].append(
                                    _thinking_entry(
                                        len(current["_tool_calls_by_id"]),
                                        entry_prev_ts,
                                        ts,
                                        encrypted=True,
                                    )
                                )
                        elif block_type == "redacted_thinking":
                            current["_thinking"].append(
                                _thinking_entry(
                                    len(current["_tool_calls_by_id"]),
                                    entry_prev_ts,
                                    ts,
                                    encrypted=True,
                                )
                            )
                        elif block_type == "tool_use":
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


def _stringify_tool_result(content: str | list | None) -> str:
    """Flatten a tool_result content field (str or list of blocks) to text.

    Args:
        content: The tool_result ``content`` — a string, a list of blocks
            (dicts with a ``text`` key, or bare strings), or None.

    Returns:
        str: The flattened text (empty string when there is nothing to render).
    """
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
