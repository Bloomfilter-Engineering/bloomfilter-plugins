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
        print(
            f"[bloomfilter] Created config at {config_file} — add your API key to get started."
        )

    return config_file


def resolve_api_key(project_dir):
    """Resolve the API key: project config > env var > user config."""
    project_config = os.path.join(project_dir, ".bloomfilter", "config.json")
    user_config = os.path.join(get_config_dir(), "config.json")

    if os.path.isfile(project_config):
        key = read_json_config(project_config, "api_key")
        if key:
            return key

    key = os.environ.get("BLOOMFILTER_API_KEY", "")
    if key:
        return key

    return read_json_config(user_config, "api_key")


def resolve_api_url(project_dir):
    """Resolve the API URL: env var > project config > user config > default."""
    # TODO: Remove hardcoded localhost URL before pushing — restore normal resolution logic
    return "http://localhost:8000"

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
# Batch file helpers
# ---------------------------------------------------------------------------


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
        f.write(line)
    if platform.system() != "Windows":
        os.chmod(batch_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def read_batch(session_id):
    """Read all entries from the batch file and return the list (no delete)."""
    batch_file = get_batch_file(session_id)
    if not os.path.isfile(batch_file):
        return []
    with open(batch_file, "r") as f:
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
    """Re-write entries back to the batch file (used on upload failure)."""
    batch_file = get_batch_file(session_id)
    with open(batch_file, "w") as f:
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
# Copilot transcript discovery and parsing
# ---------------------------------------------------------------------------

# Cache: session_id → transcript file path (avoid re-searching per hook)
_transcript_cache = {}


def find_copilot_transcript(session_id):
    """Find the Copilot transcript file that contains the given session_id.

    Searches VS Code storage locations for JSONL transcript files and scans
    for the session_id in the file content.

    Args:
        session_id: The hook session_id to search for.

    Returns:
        str: Path to the transcript file, or '' if not found.
    """
    if session_id in _transcript_cache:
        return _transcript_cache[session_id]

    if not session_id:
        return ""

    code_support = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "Code"
    )
    if not os.path.isdir(code_support):
        return ""

    search_dirs = []

    # New format: globalStorage/emptyWindowChatSessions/
    global_dir = os.path.join(
        code_support, "User", "globalStorage", "emptyWindowChatSessions"
    )
    if os.path.isdir(global_dir):
        search_dirs.append(global_dir)

    # Old format: workspaceStorage/*/GitHub.copilot-chat/transcripts/
    ws_dir = os.path.join(code_support, "User", "workspaceStorage")
    if os.path.isdir(ws_dir):
        for ws in os.listdir(ws_dir):
            transcript_dir = os.path.join(
                ws_dir, ws, "GitHub.copilot-chat", "transcripts"
            )
            if os.path.isdir(transcript_dir):
                search_dirs.append(transcript_dir)

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
                _transcript_cache[session_id] = fpath
                return fpath
        except Exception:
            continue

    return ""


def parse_copilot_transcript(transcript_path):
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

    if not transcript_path or not os.path.exists(transcript_path):
        return empty

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
        }
        for rec in reversed(records):
            if rec.get("response_content"):
                result["response_content"] = rec["response_content"]
                result["reasoning_text"] = rec.get("reasoning_text", "")
                result["input_tokens"] = rec.get("input_tokens", 0)
                result["output_tokens"] = rec.get("output_tokens", 0)
                result["model"] = (
                    rec.get("resolvedModel") or rec.get("modelId", "")
                )
                break

        return result

    except Exception:
        return empty


def _set_nested(obj, key_path, value):
    """Set *value* at *key_path* inside a nested dict/list structure.

    Each segment in *key_path* is either a ``str`` (dict key) or ``int``
    (list index).  Missing intermediate containers are created automatically.
    """
    for i, segment in enumerate(key_path[:-1]):
        next_segment = key_path[i + 1]
        if isinstance(obj, dict):
            obj = obj.setdefault(
                segment, [] if isinstance(next_segment, int) else {}
            )
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


def _reconstruct_session_state(entries):
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


def _extract_request_record(req):
    """Extract a structured record from a single materialised Copilot request.

    Returns a dict with per-request metadata, user message, response
    content, ordered reasoning parts, and token counts.
    """
    record = {
        "requestId": req.get("requestId", ""),
        "responseId": req.get("responseId", ""),
        "modelId": req.get("modelId", "").removeprefix("copilot/"),
        "resolvedModel": "",
        "userMessage": "",
        "response_content": "",
        "reasoning_text": "",
        "reasoning_parts": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "timestamp": req.get("timestamp", 0),
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
                        record["reasoning_parts"].append({
                            "type": "thinking",
                            "content": thinking["text"],
                            "thinking_id": thinking.get("id", ""),
                            "timestamp": rnd.get("timestamp", 0),
                        })

    # If toolCallRounds wasn't available (result not flushed yet), fall back
    # to the inline response[] thinking blocks.
    if not record["reasoning_parts"] and fallback_reasoning:
        for text in fallback_reasoning:
            record["reasoning_parts"].append({
                "type": "thinking",
                "content": text,
                "thinking_id": "",
                "timestamp": 0,
            })

    # Flat reasoning_text for backward compat
    all_thinking = [p["content"] for p in record["reasoning_parts"]]
    if all_thinking:
        record["reasoning_text"] = "\n".join(all_thinking)

    return record


def _parse_new_format(entries):
    """Parse the new kind-based transcript format.

    Replays CRDT entries to reconstruct the full session state, then
    extracts one record per request.

    Returns ``list[dict]`` of per-request records.
    """
    requests = _reconstruct_session_state(entries)
    return [_extract_request_record(r) for r in requests if isinstance(r, dict)]


def _parse_old_format(entries):
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
        records.append({
            "requestId": "",
            "responseId": "",
            "modelId": "",
            "resolvedModel": "",
            "userMessage": "",
            "response_content": content,
            "reasoning_text": reasoning,
            "reasoning_parts": (
                [{"type": "thinking", "content": reasoning, "thinking_id": "", "timestamp": 0}]
                if reasoning else []
            ),
            "input_tokens": 0,
            "output_tokens": 0,
            "timestamp": 0,
        })
    return records
