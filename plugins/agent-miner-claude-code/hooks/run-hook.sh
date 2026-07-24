#!/bin/sh
# POSIX launcher for the Bloomfilter hook collector.
#
# This is the macOS/Linux half of the unified plugin, and also the path taken on
# Windows when the runtime runs hooks through Git Bash (where $OS=Windows_NT). It
# keeps hooks.json readable: each hook's command is a tiny bash/PowerShell
# polyglot that just delegates here (bash side) or to run-hook.ps1 (PowerShell
# side, i.e. Windows without Git Bash).
#
# `exec` is used so the hook payload on stdin passes straight through to the
# child (python or powershell) with no extra buffering.
event="$1"

# Resolve the plugin root the runtime injects; fall back to the parent of hooks/.
root="${CLAUDE_PLUGIN_ROOT:-"$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"}"

# Windows under Git Bash / MSYS: hand off to the PowerShell launcher so Python
# discovery (python/python3/py -3) and UTF-8 stdin marshalling match the
# no-Git-Bash path instead of guessing at an interpreter here.
if [ "$OS" = "Windows_NT" ]; then
  exec powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "$root/hooks/run-hook.ps1" "$event"
fi

python="$(command -v python3 || command -v python)"
if [ -z "$python" ]; then
  # No Python on PATH: return a valid empty hook response instead of failing
  # the host hook, matching run-hook.ps1's graceful behavior.
  printf '%s\n' '{}'
  exit 0
fi

exec "$python" "$root/scripts/collect_hook.py" "$event"
