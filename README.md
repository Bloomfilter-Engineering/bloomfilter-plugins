# bloomfilter-plugins

Bloomfilter Agent Miner plugins for Claude Code and VS Code Copilot. Captures agent events (sessions, tool calls, prompts, responses) and sends them to the Bloomfilter API for observability and analysis.

## Plugins

| Plugin | Platform | Marketplace |
|--------|----------|-------------|
| `bloomfilter-agent-miner-claude-code` | Claude Code CLI | `.claude-plugin/marketplace.json` |
| `bloomfilter-agent-miner-copilot` | VS Code Copilot | `.github/plugin/marketplace.json` |

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
