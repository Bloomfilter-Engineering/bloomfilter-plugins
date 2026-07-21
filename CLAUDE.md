# bloomfilter-plugins

Monorepo of Bloomfilter "agent-miner" plugins that capture agent activity from four
coding-assistant runtimes. Each runtime has a macOS/Linux plugin and a `-windows` variant.

| Runtime | macOS/Linux plugin | Windows plugin |
| --- | --- | --- |
| Claude Code | `bloomfilter-agent-miner-claude-code` | `bloomfilter-agent-miner-claude-code-windows` |
| Codex | `agent-miner-codex` | `agent-miner-codex-windows` |
| Copilot (VS Code) | `bloomfilter-agent-miner-copilot` | `bloomfilter-agent-miner-copilot-windows` |
| Cursor | `bloomfilter-agent-miner-cursor-unified` (recommended; legacy: `-cursor`, `-cursor-windows`) | use `-cursor-unified` |

> **Install** is documented in `README.md` (Setup + Install Plugins, per runtime). This file
> covers **uninstalling** plugins during local testing — which the README does not.

## Uninstalling plugins (macOS & Windows)

CLI/marketplace commands (Claude Code, Codex) are identical on both OSes. Only the file-copy
runtime (Cursor local install) has OS-specific paths.

### Claude Code

Managed by the `claude` CLI (same command on macOS and Windows):

```bash
claude plugin uninstall bloomfilter-agent-miner-claude-code          # or ...-claude-code-windows
# optional: also drop the marketplace so it can't reinstall
claude plugin marketplace remove bloomfilter-plugins
```

`claude plugin disable <name>` turns it off without uninstalling. `claude plugin list` shows
what's installed. Restart Claude Code to apply.

### Codex

Managed by the `codex` CLI (same command on both OSes):

```bash
codex plugin remove agent-miner-codex                 # or agent-miner-codex-windows
# optional: also remove the local marketplace
codex plugin marketplace remove bloomfilter-plugins
```

Alternatively disable in `~/.codex/config.toml` (macOS/Linux) / `%USERPROFILE%\.codex\config.toml`
(Windows): set `[plugins."agent-miner-codex@bloomfilter-plugins"] enabled = false`. Restart Codex.

### Copilot (VS Code)

No CLI — uninstall through the editor:

1. Extensions view (`Cmd+Shift+X` / `Ctrl+Shift+X`), search `@agentPlugins`, right-click the
   plugin → **Uninstall**.
2. Remove the marketplace entry from VS Code `settings.json` so it can't reinstall:
   `"chat.plugins.marketplaces": ["Bloomfilter-Engineering/bloomfilter-plugins"]` → delete it.

Same steps on macOS and Windows.

### Cursor

**Marketplace install (Teams/Enterprise):** remove it from Cursor → Plugins panel.

**Local install (Free/Pro dev):** delete the copied plugin directory, then run
**Developer: Reload Window** in Cursor.

macOS/Linux:
```bash
rm -rf ~/.cursor/plugins/local/agent-miner-cursor-unified
```

Windows (PowerShell):
```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.cursor\plugins\local\agent-miner-cursor-unified"
```

Replace the trailing dir name for the legacy plugins (`agent-miner-cursor`,
`agent-miner-cursor-windows`) if those were installed. Install only ONE Cursor plugin at a time —
multiple cause duplicate event capture.

## Refreshing after local edits

Every runtime loads the plugin from an **installed copy/snapshot**, not directly from this repo,
so editing files here does nothing until you refresh that install. Two kinds of edits behave
differently:

- **`scripts/*.py`** — Python hooks are spawned fresh on every hook fire, so once the *installed*
  copy is up to date, edits take effect on the next event with no restart.
- **`plugin.json` / `hooks/hooks.json` / manifests** — read at load time, so they require a
  reload/restart (and, for the copy-based runtimes, a re-copy) to apply.

> For Codex and Claude Code, local edits are only visible if the plugin was installed from a
> **local** marketplace (`add <local-repo-path>`), not the GitHub remote the README uses. Add the
> local path first (see per-runtime steps below).

### Claude Code

```bash
# one-time: point at the working tree instead of the GitHub remote
claude plugin marketplace add /path/to/bloomfilter-plugins

# after each edit:
claude plugin marketplace update bloomfilter-plugins
claude plugin update bloomfilter-agent-miner-claude-code   # or ...-claude-code-windows
```

Restart Claude Code to apply manifest/`hooks.json` changes. Same commands on macOS and Windows.

### Codex

Codex caches the marketplace snapshot, so re-point it at the repo to pull edits:

```bash
codex plugin marketplace remove bloomfilter-plugins
codex plugin marketplace add /path/to/bloomfilter-plugins   # source_type=local; auto-re-enables the plugin
```

Restart Codex. (`codex plugin marketplace upgrade` only refreshes *Git* snapshots, not local ones.)
Same commands on both OSes.

### Copilot (VS Code)

VS Code loads from its install dir, not the repo, so re-sync the edited files, then just fire the
next hook — no editor restart needed (Python scripts spawn fresh):

macOS/Linux:
```bash
SRC=/path/to/bloomfilter-plugins/plugins/agent-miner-copilot
DST=~/.vscode/agent-plugins/github.com/Bloomfilter-Engineering/bloomfilter-plugins/plugins/agent-miner-copilot
cp "$SRC"/scripts/*.py "$DST"/scripts/
```

Windows (PowerShell): copy `scripts\*.py` from the repo into the matching
`%USERPROFILE%\.vscode\agent-plugins\...\agent-miner-copilot-windows\scripts\` install dir.

> Verify the exact install dir first (`diff -q` the repo vs. the install copy) — the path above was
> observed during debugging and the host app may relocate it. Manifest/`hooks.json` changes still
> need a full reinstall via the Extensions view.

### Cursor

Local install is a **directory copy, not a symlink**, so re-copy the whole plugin dir and reload:

macOS/Linux:
```bash
# Remove the installed copy first — cp -R into an existing dir nests instead of refreshing.
rm -rf ~/.cursor/plugins/local/agent-miner-cursor-unified
cp -R /path/to/bloomfilter-plugins/plugins/agent-miner-cursor-unified \
      ~/.cursor/plugins/local/agent-miner-cursor-unified
```

Windows (PowerShell):
```powershell
# Remove the installed copy first — Copy-Item -Recurse into an existing dir nests instead of refreshing.
Remove-Item -Recurse -Force "$env:USERPROFILE\.cursor\plugins\local\agent-miner-cursor-unified" -ErrorAction SilentlyContinue
Copy-Item -Recurse -Force "C:\path\to\bloomfilter-plugins\plugins\agent-miner-cursor-unified" `
  "$env:USERPROFILE\.cursor\plugins\local\agent-miner-cursor-unified"
```

Then run **Developer: Reload Window** in Cursor. Do not symlink — Cursor's loader did not pick up
a symlinked plugin dir in practice.

## Shared config

All plugins read one config: macOS `~/.config/bloomfilter/config.json`,
Windows `%APPDATA%\bloomfilter\config.json` (or the `BLOOMFILTER_API_KEY` env var). Local batch
output lands in the sibling `batches/` dir.
