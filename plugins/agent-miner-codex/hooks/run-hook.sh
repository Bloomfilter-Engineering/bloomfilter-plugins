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
if [ "${OS:-}" = "Windows_NT" ]; then
  exec powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "$root/hooks/run-hook.ps1" "$event"
fi

# Name existence is not enough: an asdf/pyenv/mise shim resolves as a name but
# fails at exec when no version is set. Probe by actually running each candidate,
# requiring Python >= 3.10 (the collector floor) AND that the collector's own
# module-top imports resolve. collect_hook.py imports json/subprocess/urllib.request
# at import time -- before its exit-0 guard, and with no shell fallback after exec --
# so a stripped interpreter that passed a lighter probe would ImportError into the
# exact hook error we fix. Keep this set == the collector's unguarded top imports.
_bf_probe() {  # $1 = interpreter; stdin from /dev/null so it can't eat the payload
  "$1" -c 'import sys, json, subprocess, urllib.request; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
    </dev/null >/dev/null 2>&1
}

# Recover a usable interpreter: try the canonical names, then walk PATH past a
# broken shim. Only ever run ABSOLUTE paths -- an empty or relative PATH element
# resolves against the cwd (the user's project dir), and executing a repo-local
# ./python3 is a code-exec risk we refuse to add. Cap the walk as a backstop.
_bf_find_python() {
  for _bf_name in python3 python; do
    _bf_p="$(command -v "$_bf_name" 2>/dev/null)" || continue
    case "$_bf_p" in /*) ;; *) continue ;; esac
    _bf_probe "$_bf_p" && { printf '%s\n' "$_bf_p"; return 0; }
  done
  _bf_oldifs="$IFS"; _bf_tries=0; IFS=:
  for _bf_dir in $PATH; do
    IFS="$_bf_oldifs"
    case "$_bf_dir" in /*) ;; *) IFS=:; continue ;; esac
    for _bf_name in python3 python; do
      _bf_cand="$_bf_dir/$_bf_name"
      if [ -x "$_bf_cand" ]; then
        _bf_tries=$((_bf_tries + 1))
        _bf_probe "$_bf_cand" && { printf '%s\n' "$_bf_cand"; return 0; }
        [ "$_bf_tries" -ge 12 ] && break 2
      fi
    done
    IFS=:
  done
  IFS="$_bf_oldifs"; return 1
}

# `|| true` keeps the graceful {} contract intrinsic even under an inherited
# `set -e`, where a non-zero from the resolver would otherwise abort the script
# before the fallback runs and re-surface the very hook error we are fixing.
python="$(_bf_find_python)" || true
if [ -z "$python" ]; then
  # No usable Python: return a valid empty hook response instead of failing the
  # host hook, matching run-hook.ps1's graceful behavior.
  printf '%s\n' '{}'
  exit 0
fi

exec "$python" "$root/scripts/collect_hook.py" "$event"
