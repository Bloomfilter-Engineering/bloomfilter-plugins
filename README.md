# bloomfilter-plugins

Bloomfilter Agent Miner plugins for Claude Code, VS Code Copilot, and Cursor. Captures agent events (sessions, tool calls, prompts, responses) and sends them to the Bloomfilter API for observability and analysis.

## Plugins

| Plugin | Platform | Marketplace |
|--------|----------|-------------|
| `bloomfilter-agent-miner-claude-code` | Claude Code CLI | `.claude-plugin/marketplace.json` |
| `bloomfilter-agent-miner-copilot` | VS Code Copilot | `.github/plugin/marketplace.json` |
| `bloomfilter-agent-miner-cursor` | Cursor | `.cursor-plugin/marketplace.json` |

## Install

### Claude Code

#### 1. Add the plugin marketplace

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

#### 2. Install the plugin

```bash
claude plugin install bloomfilter-agent-miner-claude-code
```

#### 3. Configure your API key

```bash
mkdir -p ~/.config/bloomfilter && cat > ~/.config/bloomfilter/config.json << 'EOF'
{
  "api_key": "YOUR_API_KEY",
  "url": ""
}
EOF
```

The plugin will also create this file automatically on first run if it doesn't exist.

---

### VS Code Copilot

#### 1. Add the plugin marketplace

Open your VS Code `settings.json` and add the Bloomfilter marketplace:

```json
"chat.plugins.marketplaces": [
    "Bloomfilter-Engineering/bloomfilter-plugins"
]
```

Or open **Settings** and search for `chat.plugins.marketplaces`, then add `Bloomfilter-Engineering/bloomfilter-plugins` as an item.

#### 2. Install the plugin

1. Open the Extensions view (`Cmd+Shift+X` / `Ctrl+Shift+X`)
2. Type `@agentPlugins` in the search field
3. Find **bloomfilter-agent-miner-copilot** and select **Install**

You can also manage installed plugins from the Chat view by selecting the gear icon > **Plugins**.

#### 3. Configure your API key

```bash
mkdir -p ~/.config/bloomfilter && cat > ~/.config/bloomfilter/config.json << 'EOF'
{
  "api_key": "YOUR_API_KEY",
  "url": ""
}
EOF
```

#### 4. Start using Copilot

Open any project in VS Code with GitHub Copilot -- the plugin activates automatically.

---

### Cursor

Cursor distributes third-party plugins through **Team Marketplaces**, a feature available on the **Teams (Business)** and **Enterprise** plans. A Cursor org admin adds the Bloomfilter marketplace once; individual users then install from it.

> **Plan requirement:** Team Marketplaces require Teams or Enterprise. Free and Pro users should follow the [local development setup](#cursor-1) below.

#### 1. Admin — add the Bloomfilter marketplace (one-time, org-wide)

1. Open the **Cursor Dashboard** → **Settings** → **Plugins** → **Team Marketplaces** → **Import**.
2. Paste the GitHub URL: `https://github.com/Bloomfilter-Engineering/bloomfilter-plugins`
3. Review the parsed plugins (Cursor reads `.cursor-plugin/marketplace.json` at the repo root and lists `bloomfilter-agent-miner-cursor`). Optionally scope the marketplace to specific Team Access groups.
4. Set the marketplace name and description, then **Save**.
5. (Recommended) Mark `bloomfilter-agent-miner-cursor` as **required** so it auto-installs for every member of the selected Team Access groups.

Reference: [Cursor — Plugins docs](https://cursor.com/docs/plugins#creating-plugins).

#### 2. End-user — install the plugin

- If the admin marked the plugin **required**, it auto-installs — nothing to do.
- Otherwise, open the **Plugins** panel in Cursor, find **bloomfilter-agent-miner-cursor** under the Bloomfilter team marketplace, and click **Install**. Cursor registers the bundled `hooks/hooks.json` automatically.

#### 3. Configure your API key

```bash
mkdir -p ~/.config/bloomfilter && cat > ~/.config/bloomfilter/config.json << 'EOF'
{
  "api_key": "YOUR_API_KEY",
  "url": ""
}
EOF
```

The plugin also creates this file automatically on the first `sessionStart` hook.

#### 4. Start using Cursor's agent

Open any project and use the Cursor agent -- the plugin activates on `sessionStart` and uploads the hook batch on every `stop` and `sessionEnd`.

---

## Configuration

The config file lives at `~/.config/bloomfilter/config.json`. Both plugins share the same config.

The `url` field can be left empty -- it defaults to `https://api.bloomfilter.app`.

You can optionally add a project-level config at `{project}/.bloomfilter/config.json` for non-secret overrides like `url`. **Do not store API keys in project-level config** -- use the user config or the `BLOOMFILTER_API_KEY` environment variable.

### Environment variable overrides

- `BLOOMFILTER_API_KEY` -- override the API key
- `BLOOMFILTER_URL` -- override the API URL

## Development

### Prerequisites

- Python 3.9+
- VS Code 1.115+ with GitHub Copilot 0.43+ (for Copilot plugin)

### Local setup

#### Claude Code

```bash
claude plugin add /path/to/bloomfilter-plugins/plugins/agent-miner-claude-code
```

#### VS Code Copilot

Add to your VS Code `settings.json`:

```json
"chat.pluginLocations": {
    "/path/to/bloomfilter-plugins/plugins/agent-miner-copilot": true
}
```

#### Cursor

For plugin development, or for Cursor users on Free/Pro plans (Team Marketplaces aren't available to them), install the plugin locally. Copy it into Cursor's local-plugin directory, then reload the Cursor window (**Developer: Reload Window**):

```bash
mkdir -p ~/.cursor/plugins/local
cp -R /path/to/bloomfilter-plugins/plugins/agent-miner-cursor \
      ~/.cursor/plugins/local/agent-miner-cursor
```

Re-copy after any code change (symlinking is not reliable — Cursor's loader did not pick up a symlinked plugin dir in testing).

### Debugging

- Batch files are stored at `~/.config/bloomfilter/batches/`
- stderr output from hook scripts appears in VS Code's output channels

### API URL override for testing

Point the plugin at a local API server:

```json
{
  "api_key": "your-key",
  "url": "http://localhost:8000"
}
```

Or use an environment variable:

```bash
BLOOMFILTER_URL=http://localhost:8000
```
