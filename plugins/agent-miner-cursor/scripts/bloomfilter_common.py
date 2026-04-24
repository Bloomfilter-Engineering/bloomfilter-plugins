import contextlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

PLUGIN_VERSION = "0.1.0"
DEFAULT_API_URL = "https://api.bloomfilter.app"


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
            f"[bloomfilter] Created config at {config_file} — add your API key to get started."
        )

    return config_file


def resolve_api_key():
    """Resolve the API key: BLOOMFILTER_API_KEY env var > user config.

    Project-level config is intentionally NOT consulted for the API key —
    project configs live in the repo and can be accidentally committed.
    The user config (~/.config/bloomfilter/config.json) and the env var
    are the only supported places to store the API key.
    """
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


# ---------------------------------------------------------------------------
# Payload / stdin
# ---------------------------------------------------------------------------


def read_payload():
    """Read JSON payload from stdin."""
    if platform.system() == "Windows":
        sys.stdin.reconfigure(encoding="utf-8")
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
# Batch file helpers (with file locking for concurrent hook processes)
# ---------------------------------------------------------------------------


if platform.system() != "Windows":
    import fcntl

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
        """No-op lock on Windows."""
        yield


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


def clear_batch(session_id):
    """Delete the batch file for *session_id*."""
    batch_file = get_batch_file(session_id)
    if os.path.isfile(batch_file):
        os.remove(batch_file)


def rewrite_batch(session_id, entries):
    """Re-write entries back to the batch file."""
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


def upload_batch(api_url, api_key, payload):
    """POST raw hook batch to the Bloomfilter API. Returns True on success."""
    try:
        data = json.dumps(payload).encode("utf-8")
        url = f"{api_url}/api/agent-sessions/hooks/"
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
            resp.read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utcnow_iso():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Cursor transcript parsing
# ---------------------------------------------------------------------------

# Probe the transcript JSONL with a wide net of token field names. The file
# format is not publicly documented — this is deliberately permissive so the
# parser survives schema drift. Returns {} if nothing matches.

_INPUT_TOKEN_KEYS = (
    "input_tokens", "promptTokens", "prompt_tokens",
    "inputTokens", "tokens_in",
)
_OUTPUT_TOKEN_KEYS = (
    "output_tokens", "outputTokens", "completionTokens", "completion_tokens",
    "tokens_out",
)
_GENERATION_ID_KEYS = ("generation_id", "generationId", "generationID")
_MODEL_KEYS = ("model", "resolvedModel", "modelId", "model_id")


def _first_key(obj, keys):
    """Return the first non-empty value from *obj* for any key in *keys*."""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", 0):
            return v
    return None


def _scan_usage(obj):
    """Find input/output token counts anywhere in a nested dict.

    Checks the top level, then ``usage``, then ``metadata``, then any nested
    dict one level deep. Returns (input_tokens, output_tokens).
    """
    if not isinstance(obj, dict):
        return 0, 0

    candidates = [obj]
    for key in ("usage", "metadata", "result", "response"):
        val = obj.get(key)
        if isinstance(val, dict):
            candidates.append(val)
            nested_usage = val.get("usage")
            if isinstance(nested_usage, dict):
                candidates.append(nested_usage)

    for cand in candidates:
        in_tok = _first_key(cand, _INPUT_TOKEN_KEYS)
        out_tok = _first_key(cand, _OUTPUT_TOKEN_KEYS)
        if in_tok or out_tok:
            return int(in_tok or 0), int(out_tok or 0)

    return 0, 0


def _scan_generation_id(obj):
    """Find a generation_id anywhere shallow inside *obj*."""
    if not isinstance(obj, dict):
        return ""
    gid = _first_key(obj, _GENERATION_ID_KEYS)
    if gid:
        return str(gid)
    for key in ("payload", "request", "response", "metadata", "data"):
        nested = obj.get(key)
        gid = _first_key(nested, _GENERATION_ID_KEYS) if isinstance(nested, dict) else None
        if gid:
            return str(gid)
    return ""


def _scan_model(obj):
    """Find a model ID anywhere shallow inside *obj*."""
    if not isinstance(obj, dict):
        return ""
    model = _first_key(obj, _MODEL_KEYS)
    if model:
        return str(model)
    for key in ("metadata", "request", "response", "result"):
        nested = obj.get(key)
        model = _first_key(nested, _MODEL_KEYS) if isinstance(nested, dict) else None
        if model:
            return str(model)
    return ""


def parse_cursor_transcript(transcript_path):
    """Parse a Cursor transcript JSONL file and return per-turn token data.

    Returns a dict keyed by ``generation_id`` with values
    ``{ "input_tokens": int, "output_tokens": int, "model": str }``.
    Returns ``{}`` on any error — the caller is expected to upload the batch
    regardless so the BE can fall back to a token estimator.

    The Cursor transcript format is not publicly documented; this parser is
    deliberately defensive and probes a wide set of field names.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return {}

    try:
        file_size = os.path.getsize(transcript_path)
        read_start = max(0, file_size - 200_000)
        with open(transcript_path, "rb") as tf:
            if read_start > 0:
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

        if not entries:
            return {}

        results = {}
        for entry in entries:
            gid = _scan_generation_id(entry)
            if not gid:
                continue
            in_tok, out_tok = _scan_usage(entry)
            if not in_tok and not out_tok:
                continue
            model = _scan_model(entry)
            # Keep the record with the highest output_tokens for a given
            # generation_id (streaming produces incremental snapshots).
            prev = results.get(gid)
            if prev and prev.get("output_tokens", 0) >= out_tok:
                continue
            results[gid] = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "model": model,
            }

        return results

    except Exception:
        return {}
