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
from typing import IO, Any, Iterator

# Platform-specific stdlib modules used by ``_lock_file`` below.
if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.1.4"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"
DEBUG_LOG_TAG = "claude-code-windows"  # disambiguates plugins sharing the same log dir

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

# Poll interval while waiting for the upload slot.
SLOT_POLL_INTERVAL_S = 0.1


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


def append_to_batch(session_id: str, entry: dict[str, Any]) -> None:
    """Append one envelope to a session's batch file as a JSON line.

    Args:
        session_id: Session the entry belongs to.
        entry: The hook envelope to persist. Must be JSON-serializable.
    """
    batch_file_path = get_batch_file(session_id)
    serialized_entry = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(batch_file_path, "a") as batch_file_handle:
        with _lock_file(batch_file_handle, exclusive=True):
            batch_file_handle.write(serialized_entry)
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


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
    if platform.system() != "Windows":
        os.chmod(batch_file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


# ---------------------------------------------------------------------------
# HTTP upload
# ---------------------------------------------------------------------------


def upload_batch(api_url: str, api_key: str, payload: dict[str, Any]) -> bool:
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
        True if the server answered 2xx, meaning the records are safe to drain.
        False for an invalid URL, an unserializable payload, a transport error,
        or any non-2xx status — in which case the caller must keep the records.
    """
    parsed_api_url = urllib.parse.urlparse(api_url or "")
    if parsed_api_url.scheme not in ("http", "https") or not parsed_api_url.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        print(
            "[bloomfilter] Upload skipped: invalid Bloomfilter API URL.",
            file=sys.stderr,
        )
        return False

    endpoint_url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    record_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0

    try:
        encoded_payload = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError) as serialization_error:
        debug_log(
            f"upload_batch: skipped — payload not JSON-serializable "
            f"session_id={session_id} "
            f"error={type(serialization_error).__name__}: {serialization_error}"
        )
        return False

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
        if status_code != 201:
            print(
                f"[bloomfilter] Upload response status: {status_code}",
                file=sys.stderr,
            )
        return 200 <= status_code < 300
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
        stderr_message = f"[bloomfilter] Upload failed with HTTP {http_error.code}"
        if failure_reason:
            stderr_message += f" {failure_reason}"
        print(stderr_message, file=sys.stderr)
        if error_body:
            print(
                f"[bloomfilter] Upload response body: {error_body[:500]}",
                file=sys.stderr,
            )
        return False
    except urllib.error.URLError as url_error:
        debug_log(
            f"upload_batch: URLError session_id={session_id} "
            f"reason={url_error.reason!r}"
        )
        print(f"[bloomfilter] Upload failed: {url_error.reason}", file=sys.stderr)
        return False
    except Exception as unexpected_error:
        debug_log(
            f"upload_batch: error session_id={session_id} "
            f"type={type(unexpected_error).__name__} message={unexpected_error!s}"
        )
        print(f"[bloomfilter] Upload failed: {unexpected_error}", file=sys.stderr)
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


def _cap_text(value: str) -> str:
    """Truncate a string to the subagent field cap; return it unchanged otherwise.

    Args:
        value: The string to cap.

    Returns:
        str: The string, truncated to _SUBAGENT_FIELD_CAP chars if longer.
    """
    if len(value) > _SUBAGENT_FIELD_CAP:
        return value[:_SUBAGENT_FIELD_CAP] + "…[truncated]"
    return value


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
            return turn

        for entry in entries:
            entry_type = entry.get("type")
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
