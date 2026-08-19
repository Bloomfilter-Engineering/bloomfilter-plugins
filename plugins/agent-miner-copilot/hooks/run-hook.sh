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
#
# Inert for Copilot in practice — VS Code runs hooks through ComSpec (Windows
# PowerShell) and the Copilot CLI runs them through powershell.exe on win32, so
# neither ever reaches this branch. Kept so this file stays in step with the
# claude-code/codex copies.
if [ "${OS:-}" = "Windows_NT" ] && command -v powershell.exe >/dev/null 2>&1; then
  # Convert the POSIX script path so `powershell.exe -File` can open it (Git Bash /
  # MSYS / Cygwin). When powershell.exe is absent the guard above skips delegation
  # and the POSIX discovery below runs instead, so the hook still fails soft.
  _bf_ps1="$root/hooks/run-hook.ps1"
  if command -v cygpath >/dev/null 2>&1; then
    _bf_ps1="$(cygpath -w "$_bf_ps1" 2>/dev/null || printf '%s' "$_bf_ps1")"
  fi
  exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$_bf_ps1" "$event"
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
  IFS="$_bf_oldifs"

  # PATH is not the whole machine. A hook host need not pass the user's
  # interactive PATH, and on a stock macOS the only interpreter on a minimal one
  # is /usr/bin/python3 -- which is 3.9, below the floor -- while the 3.10+ the
  # user actually installed sits in Homebrew, asdf, or the python.org framework.
  # Searching PATH alone therefore finds nothing usable and the hook captures
  # nothing at all, on a machine that is perfectly well provisioned.
  #
  # Absolute paths only, same rule as above, and the framework glob is expanded
  # newest-first so a current interpreter is preferred over an old one.
  for _bf_cand in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$HOME/.asdf/shims/python3" \
    "$HOME/.pyenv/shims/python3" \
    "$HOME/.local/bin/python3" \
    /opt/local/bin/python3
  do
    case "$_bf_cand" in /*) ;; *) continue ;; esac
    [ -x "$_bf_cand" ] || continue
    _bf_probe "$_bf_cand" && { printf '%s\n' "$_bf_cand"; return 0; }
  done
  for _bf_cand in $(ls -d /Library/Frameworks/Python.framework/Versions/3.* 2>/dev/null | sort -r)
  do
    [ -x "$_bf_cand/bin/python3" ] || continue
    _bf_probe "$_bf_cand/bin/python3" && {
      printf '%s\n' "$_bf_cand/bin/python3"; return 0; }
  done
  return 1
}

# With no interpreter there is no collector, and the collector is what writes
# debug.log -- so this failure is otherwise completely silent: no data, and
# nothing anywhere to say why. Leave the one line from the shell instead.
_bf_note_no_python() {
  _bf_note_dir="${XDG_CONFIG_HOME:-$HOME/.config}/bloomfilter"
  mkdir -p "$_bf_note_dir" 2>/dev/null || return 0
  printf '%sZ [%s] hook skipped: reason=no-python-3.10-found event=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S 2>/dev/null || printf 'unknown-time')" \
    "$(basename "$root")" "$event" >> "$_bf_note_dir/debug.log" 2>/dev/null || true
}

# `|| true` keeps the graceful {} contract intrinsic even under an inherited
# `set -e`, where a non-zero from the resolver would otherwise abort the script
# before the fallback runs and re-surface the very hook error we are fixing.
python="$(_bf_find_python)" || true
if [ -z "$python" ]; then
  # No usable Python: return a valid empty hook response instead of failing the
  # host hook, matching run-hook.ps1's graceful behavior. Record why first —
  # otherwise the only symptom is an absence of data with nothing to explain it,
  # because the thing that writes debug.log is the collector we cannot start.
  _bf_note_no_python
  printf '%s\n' '{}'
  exit 0
fi

# Run (not exec) so a non-zero collector exit still yields {} instead of a hook
# error -- the collector always exits 0 by design, so this makes the fail-soft
# contract unconditional and mirrors run-hook.ps1. The SessionEnd detached uploader
# redirects its own stdout to DEVNULL, so it never holds this captured pipe open.
if _bf_out="$("$python" "$root/scripts/collect_hook.py" "$event")" && [ -n "$_bf_out" ]; then
  printf '%s\n' "$_bf_out"
else
  printf '%s\n' '{}'
fi
