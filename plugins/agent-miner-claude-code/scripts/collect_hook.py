#!/usr/bin/env python3
"""Universal hook handler for Bloomfilter agent mining.

Collects raw hook payloads, batches them in a JSONL file, and uploads
the batch to the Bloomfilter API on Stop and SessionEnd events.
"""

import os
import sys

# Ensure the scripts directory is on the path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bloomfilter_common import (
    PLUGIN_VERSION,
    append_to_batch,
    bootstrap_config,
    clear_batch,
    extract_transcript_summary,
    get_git_branch,
    read_batch,
    read_payload,
    resolve_api_key,
    resolve_api_url,
    upload_batch,
    utcnow_iso,
)

# Hooks that trigger an upload to the BE
UPLOAD_HOOKS = {"Stop", "SessionEnd"}

# Hooks where we fetch the current git branch
GIT_BRANCH_HOOKS = {"SessionStart", "UserPromptSubmit"}

# Hooks where we extract transcript token summary
# Stop: current turn tokens; UserPromptSubmit: backfill previous turn if Stop missed tokens
TRANSCRIPT_HOOKS = {"Stop", "UserPromptSubmit"}


def main():
    hook_event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    if not hook_event_name:
        return

    payload = read_payload()
    session_id = payload.get("session_id", "")
    if not session_id:
        return

    project_dir = payload.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # On SessionStart: bootstrap config and check for API key
    if hook_event_name == "SessionStart":
        bootstrap_config(plugin_root)
        api_key = resolve_api_key()
        if not api_key:
            return

    # Build the envelope — raw payload passed through untouched
    envelope = {
        "hook_event_name": hook_event_name,
        "received_at": utcnow_iso(),
        "plugin_version": PLUGIN_VERSION,
        "payload": payload,
    }

    # Fetch git branch only on specific hooks (avoid subprocess overhead)
    if hook_event_name in GIT_BRANCH_HOOKS and project_dir:
        envelope["git_branch"] = get_git_branch(project_dir)

    # Extract transcript token summary on Stop
    if hook_event_name in TRANSCRIPT_HOOKS:
        transcript_path = payload.get("transcript_path", "")
        summary = extract_transcript_summary(transcript_path)
        if summary:
            envelope["transcript_summary"] = summary

    # Append to batch file
    append_to_batch(session_id, envelope)

    # Upload on Stop/SessionEnd
    if hook_event_name in UPLOAD_HOOKS:
        api_key = resolve_api_key()
        if not api_key:
            return

        api_url = resolve_api_url()
        entries = read_batch(session_id)
        if not entries:
            return

        batch_payload = {
            "session_id": session_id,
            "source": "claude_code",
            "plugin_version": PLUGIN_VERSION,
            "hooks": entries,
        }

        success = upload_batch(api_url, api_key, batch_payload)
        if success and hook_event_name == "SessionEnd":
            # Only delete batch file on SessionEnd success
            clear_batch(session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Never block Claude
    sys.exit(0)
