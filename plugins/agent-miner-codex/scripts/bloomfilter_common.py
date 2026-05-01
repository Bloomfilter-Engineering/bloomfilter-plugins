import contextlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl

PLUGIN_VERSION = "0.1.0"
DEFAULT_API_URL = "https://api.bloomfilter.app"
DEBUG_LOG_NAME = "debug.log"


def _resolve_debug_log_dir():
    """Return the directory for debug.log.

    Codex injects ${PLUGIN_DATA} / ${CLAUDE_PLUGIN_DATA} into hook env, pointing
    at ~/.codex/plugins/data/<marketplace>/<plugin>/. Use that when present so
    diagnostic logs live alongside other plugin data per Codex convention.
    Fall back to the bloomfilter config dir for non-hook invocations
    (manual tests, future tools).
    """
    return (
        os.environ.get("PLUGIN_DATA")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or get_config_dir()
    )


def debug_log(msg):
    """Append a timestamped line to <plugin-data>/debug.log.

    Always writes — silent on failure. Intended for ops/diagnostic visibility
    of hook firing and upload responses without polluting Codex's TUI stderr.
    """
    try:
        log_dir = _resolve_debug_log_dir()
        secure_makedirs(log_dir)
        log_path = os.path.join(log_dir, DEBUG_LOG_NAME)
        line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
        with open(log_path, "a") as f:
            f.write(line)
        if platform.system() != "Windows":
            os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        # Logger must never crash the hook.
        pass


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


def read_json_config(path, key, default=""):
    """Safely read a single key from a JSON config file."""
    try:
        with open(path, "r") as f:
            return json.load(f).get(key, default) or default
    except Exception:
        return default


def bootstrap_config(plugin_root):
    """Create the user config from the plugin template if it does not exist."""
    config_dir = get_config_dir()
    config_file = os.path.join(config_dir, "config.json")
    template = os.path.join(plugin_root, "bloomfilter.config.json")

    if not os.path.isfile(config_file):
        secure_makedirs(config_dir)
        if os.path.isfile(template):
            shutil.copy2(template, config_file)
        else:
            with open(config_file, "w") as f:
                json.dump({"api_key": "", "url": ""}, f, indent=2)
                f.write("\n")
        if platform.system() != "Windows":
            os.chmod(config_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    return config_file


def resolve_api_key():
    """Resolve the API key from env var or user config only."""
    key = os.environ.get("BLOOMFILTER_API_KEY", "")
    if key:
        return key

    user_config = os.path.join(get_config_dir(), "config.json")
    return read_json_config(user_config, "api_key")


def resolve_api_url(project_dir):
    """Resolve the API URL: env var > project config > user config > default."""
    env_url = os.environ.get("BLOOMFILTER_URL", "")
    if env_url:
        return env_url

    project_config = os.path.join(project_dir, ".bloomfilter", "config.json")
    user_config = os.path.join(get_config_dir(), "config.json")

    if os.path.isfile(project_config):
        url = read_json_config(project_config, "url")
        if url:
            return url

    url = read_json_config(user_config, "url")
    if url:
        return url

    return DEFAULT_API_URL


def read_payload():
    """Read a JSON hook payload from stdin."""
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8")
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


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
        """Cross-process byte-range lock on Windows via msvcrt.locking."""
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
    """Return and create the Bloomfilter hook batch directory."""
    batch_dir = os.path.join(get_config_dir(), "batches")
    secure_makedirs(batch_dir)
    return batch_dir


def get_batch_file(session_id):
    """Return path to the JSONL batch file for session_id."""
    safe_id = os.path.basename(session_id)
    if not safe_id or safe_id != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return os.path.join(get_batch_dir(), f"{safe_id}.jsonl")


def append_to_batch(session_id, entry):
    """Append one JSON object to the session batch file."""
    batch_file = get_batch_file(session_id)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with open(batch_file, "a") as f:
        with _lock_file(f, exclusive=True):
            f.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def read_batch(session_id):
    """Read all valid JSON entries from a session batch file."""
    batch_file = get_batch_file(session_id)
    if not os.path.isfile(batch_file):
        return []
    with open(batch_file, "r") as f:
        with _lock_file(f, exclusive=False):
            lines = f.readlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def rewrite_batch(session_id, entries):
    """Rewrite a session batch while holding an exclusive lock."""
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
    """Clear a session batch without deleting the coordination file."""
    rewrite_batch(session_id, [])


def upload_batch(api_url, api_key, payload):
    """POST a raw hook batch to the Bloomfilter API.

    Logs request URL, response status, and (truncated) body to the debug log.
    Returns True on 2xx, False otherwise.
    """
    parsed = urllib.parse.urlparse(api_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        debug_log(f"upload_batch: skipped — invalid api_url={api_url!r}")
        return False

    url = f"{api_url.rstrip('/')}/api/agent-sessions/hooks/"
    session_id = payload.get("session_id", "?") if isinstance(payload, dict) else "?"
    hook_count = len(payload.get("hooks", [])) if isinstance(payload, dict) else 0
    debug_log(
        f"upload_batch: POST {url} session_id={session_id} hooks={hook_count}"
    )

    try:
        data = json.dumps(payload).encode("utf-8")
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
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        debug_log(
            f"upload_batch: response status={status} body={body[:500]!r}"
        )
        return 200 <= status < 300
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        debug_log(
            f"upload_batch: HTTPError status={e.code} body={body[:500]!r}"
        )
        return False
    except Exception as e:
        debug_log(f"upload_batch: error={type(e).__name__}: {e}")
        return False


def utcnow_iso():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
