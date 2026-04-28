#!/usr/bin/env python3
"""Install Bloomfilter Codex hooks into ~/.codex/hooks.json.

Codex 0.124 ignores the `hooks` field on plugin manifests, so plugin-bundled
hooks.json files are not auto-registered. This script reads the bundled
hooks.json template, expands placeholders to absolute paths, and merges the
entries into the user's ~/.codex/hooks.json so they fire on every Codex
session. Run again after upgrading the plugin to refresh paths.

Usage:
    python install_codex_hooks.py            # install / refresh
    python install_codex_hooks.py --uninstall # remove only Bloomfilter entries
"""

import argparse
import json
import os
import sys
from copy import deepcopy

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(PLUGIN_ROOT, "hooks.json")
USER_HOOKS = os.path.expanduser("~/.codex/hooks.json")
MARKER = "bloomfilter-agent-miner-codex"


def _materialize_template():
    with open(TEMPLATE) as f:
        data = json.load(f)
    serialized = json.dumps(data)
    serialized = serialized.replace("${CODEX_PLUGIN_ROOT}", PLUGIN_ROOT)
    serialized = serialized.replace(
        "$(command -v python3 || command -v python)", sys.executable
    )
    return json.loads(serialized)


def _is_ours(entry):
    for hook in entry.get("hooks", []) or []:
        if MARKER in hook.get("command", ""):
            return True
    return False


def _strip_ours(existing):
    hooks = existing.get("hooks", {}) or {}
    for event, entries in list(hooks.items()):
        kept = [e for e in entries if not _is_ours(e)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    existing["hooks"] = hooks
    return existing


def _merge(existing, ours):
    existing.setdefault("hooks", {})
    for event, entries in ours.get("hooks", {}).items():
        existing["hooks"].setdefault(event, []).extend(deepcopy(entries))
    return existing


def _load_existing():
    if not os.path.isfile(USER_HOOKS):
        return {}
    try:
        with open(USER_HOOKS) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        backup = USER_HOOKS + ".bak"
        os.rename(USER_HOOKS, backup)
        print(f"[bloomfilter] {USER_HOOKS} was unparseable; backed up to {backup}")
        return {}


def _write(data):
    os.makedirs(os.path.dirname(USER_HOOKS), exist_ok=True)
    with open(USER_HOOKS, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove Bloomfilter entries from ~/.codex/hooks.json without adding new ones.",
    )
    args = parser.parse_args()

    existing = _load_existing()
    existing = _strip_ours(existing)

    if args.uninstall:
        _write(existing)
        print(f"[bloomfilter] Removed Bloomfilter hooks from {USER_HOOKS}")
        return

    ours = _materialize_template()
    merged = _merge(existing, ours)
    _write(merged)
    print(f"[bloomfilter] Installed hooks into {USER_HOOKS}")
    print(f"[bloomfilter] Plugin root: {PLUGIN_ROOT}")
    print(f"[bloomfilter] Python:      {sys.executable}")


if __name__ == "__main__":
    main()
