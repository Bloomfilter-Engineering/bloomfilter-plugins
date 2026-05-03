# bloomfilter-plugins

Bloomfilter Agent Miner plugins for Claude Code, VS Code Copilot, and Cursor. The plugins capture agent events such as sessions, prompts, tool calls, responses, and session stops, then send them to Bloomfilter for observability and analysis.

## Available Plugins

| Plugin | Platform | Marketplace |
|--------|----------|-------------|
| `bloomfilter-agent-miner-claude-code` | Claude Code CLI | `.claude-plugin/marketplace.json` |
| `agent-miner-codex` | Codex CLI and desktop app | `.agents/plugins/marketplace.json` |
| `bloomfilter-agent-miner-copilot` | VS Code Copilot | `.github/plugin/marketplace.json` |
| `bloomfilter-agent-miner-cursor` | Cursor on macOS and Linux | `.cursor-plugin/marketplace.json` |
| `bloomfilter-agent-miner-cursor-windows` | Cursor on Windows | `.cursor-plugin/marketplace.json` |

## Setup

Do this once on each machine before installing a plugin.

### Dependencies

- Python 3.13 or newer. The hook scripts use only Python standard library modules, so there are no Python packages to install.
- A Bloomfilter API key.
- Git, if you want Git branch metadata captured. The plugins still work without Git.
- The host application for the plugin you want to use:
  - Claude Code CLI for `bloomfilter-agent-miner-claude-code`.
  - Codex CLI or Codex desktop app for `agent-miner-codex`.
  - VS Code 1.115+ for `bloomfilter-agent-miner-copilot`.
  - Cursor 3.2.16+ with Plugins support for `bloomfilter-agent-miner-cursor` or `bloomfilter-agent-miner-cursor-windows`.

### macOS Dependencies

Check whether Python is already installed:

```bash
python3 --version || python --version
```

If Python is missing, install it with Homebrew:

```bash
brew install python
```

If you do not have Homebrew, install Python from <https://www.python.org/downloads/macos/>.

Check whether Git is already installed:

```bash
git --version
```

If Git is missing, install Apple's command line tools:

```bash
xcode-select --install
```

You can also install Git with Homebrew:

```bash
brew install git
```

### Windows Dependencies

Check whether Python is already installed:

```powershell
python3 --version
python --version
```

If either command prints Python 3.10 or newer, you are set. If Python is missing, install it with `winget`:

```powershell
winget install Python.Python.3.12
```

You can also install Python from <https://www.python.org/downloads/windows/>. During installation, enable **Add python.exe to PATH**.

Check whether Git is already installed:

```powershell
git --version
```

If Git is missing, install it with `winget`:

```powershell
winget install Git.Git
```

You can also install Git from <https://git-scm.com/download/win>. Restart PowerShell, Cursor, VS Code, or Claude Code after installing Python or Git so the updated `PATH` is available.

## Configure Your API Key

All Bloomfilter Agent Miner plugins share the same user-level config file. You only need to create it once per machine.

### macOS

```bash
mkdir -p ~/.config/bloomfilter
cat > ~/.config/bloomfilter/config.json << 'EOF'
{
  "api_key": "YOUR_API_KEY"
}
EOF
chmod 600 ~/.config/bloomfilter/config.json
```

### Windows

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\bloomfilter"
@'
{
  "api_key": "YOUR_API_KEY"
}
'@ | Set-Content -Encoding UTF8 "$env:APPDATA\bloomfilter\config.json"
```

You can also provide the API key with an environment variable.

macOS:

```bash
export BLOOMFILTER_API_KEY="YOUR_API_KEY"
```

Windows PowerShell:

```powershell
$env:BLOOMFILTER_API_KEY = "YOUR_API_KEY"
```

To make the Windows environment variable persistent:

```powershell
[Environment]::SetEnvironmentVariable("BLOOMFILTER_API_KEY", "YOUR_API_KEY", "User")
```

Restart your agent application after changing persistent environment variables.

## Install Plugins

Choose the plugin for the agent platform you use.

### Claude Code

Add the Bloomfilter plugin marketplace:

```bash
claude plugin marketplace add Bloomfilter-Engineering/bloomfilter-plugins
```

Or add it manually to your Claude Code settings:

```json
{
  "plugins": {
    "marketplaces": ["Bloomfilter-Engineering/bloomfilter-plugins"]
  }
}
```

Install the plugin:

```bash
claude plugin install bloomfilter-agent-miner-claude-code
```

Open any project in Claude Code. The plugin creates the config file automatically on first run if it does not exist, but you still need to add your API key.

### Codex

Codex hooks are behind feature flags. Enable them before installing the plugin:

```bash
codex features enable codex_hooks
codex features enable plugin_hooks
```

These commands update `~/.codex/config.toml`. You can also edit the file manually:

```toml
[features]
codex_hooks = true
plugin_hooks = true
```

If your config already has a `[features]` table, add only the two keys inside the existing table when editing manually.

Add the Bloomfilter plugin marketplace with the Codex CLI:

```bash
codex plugin marketplace add Bloomfilter-Engineering/bloomfilter-plugins
```

Open Codex, find **Bloomfilter Agent Miner for Codex** in the plugin marketplace, and install `agent-miner-codex`. Restart Codex after enabling the feature flags or installing the plugin so hook registration is reloaded.

Note: Codex thinking text is encrypted by Codex and is not readable by this plugin.

### VS Code Copilot

Open your VS Code `settings.json` and add the Bloomfilter marketplace:

```json
"chat.plugins.marketplaces": [
  "Bloomfilter-Engineering/bloomfilter-plugins"
]
```

Or open **Settings**, search for `chat.plugins.marketplaces`, and add `Bloomfilter-Engineering/bloomfilter-plugins` as an item.

Install the plugin:

1. Open the Extensions view with `Cmd+Shift+X` on macOS or `Ctrl+Shift+X` on Windows.
2. Type `@agentPlugins` in the search field.
3. Find **bloomfilter-agent-miner-copilot** and select **Install**.

Open any project in VS Code with GitHub Copilot. The plugin activates automatically.

### Cursor

Cursor distributes third-party plugins through **Team Marketplaces**, a feature available on Teams and Enterprise plans. A Cursor org admin adds the Bloomfilter marketplace once, then users install from it.

Bloomfilter publishes separate Cursor plugins by operating system:

- Use `bloomfilter-agent-miner-cursor` on macOS and Linux.
- Use `bloomfilter-agent-miner-cursor-windows` on Windows.

Admin setup:

1. Open the **Cursor Dashboard**.
2. Go to **Settings** > **Plugins** > **Team Marketplaces** > **Import**.
3. Paste `https://github.com/Bloomfilter-Engineering/bloomfilter-plugins`.
4. Confirm that Cursor finds both `bloomfilter-agent-miner-cursor` and `bloomfilter-agent-miner-cursor-windows`.
5. Save the marketplace.
6. Optional but recommended: mark the correct OS-specific plugin as **required** so it installs automatically for selected Team Access groups.

User setup:

- If the plugin is required by your admin, it installs automatically.
- Otherwise, open Cursor's **Plugins** panel, find the plugin for your operating system in the Bloomfilter team marketplace, and click **Install**.

For local development, or for Cursor users on Free or Pro plans, copy the plugin into Cursor's local plugin directory.

macOS:

```bash
mkdir -p ~/.cursor/plugins/local
cp -R /path/to/bloomfilter-plugins/plugins/agent-miner-cursor \
  ~/.cursor/plugins/local/agent-miner-cursor
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.cursor\plugins\local"
Copy-Item -Recurse -Force `
  "C:\path\to\bloomfilter-plugins\plugins\agent-miner-cursor-windows" `
  "$env:USERPROFILE\.cursor\plugins\local\agent-miner-cursor-windows"
```

Reload Cursor after installing or copying the plugin by running **Developer: Reload Window** from the Command Palette.

## Verify and Debug

Start a session in your agent application, send a prompt, let the agent respond, then stop or end the session. Confirm that data appears in Bloomfilter.

Local batch files are written before upload:

- macOS: `~/.config/bloomfilter/batches/`
- Windows: `%APPDATA%\bloomfilter\batches\`

If data does not appear in Bloomfilter, check that:

- Python is available from the agent application's environment. Run `python3 --version` or `python --version` to confirm.
- Your user config contains a valid `api_key`.
- The plugin is installed and the agent application was restarted or reloaded after installation.
