# AI Rule Miner

AI Rule Miner is a tool that mines your AI chat history (Claude, Codex, Cursor, Windsurf) for cross-project correction patterns. It automatically identifies when you have corrected the AI's behavior, calculates a recency-weighted score, and generates a list of standard rules (or updates your MCP server) to prevent the AI from making the same mistakes again.

## Installation

You can install this tool globally using `pip`:

```bash
pip install -e .
```

This installs two global commands:
- `ai-rule-miner`: The core CLI tool.
- `ai-rule-miner-mcp`: The MCP server for IDEs and AI assistants.

## CLI Usage

Run the miner against your default AI history (Claude is the default):

```bash
ai-rule-miner
```

### Configuration

You can fully customize the miner's stop words, retention windows, and scoring weights. The tool uses a global config file (`~/.claude/miner_config.json`).

Generate the default configuration file:
```bash
ai-rule-miner --init-config
```

You can manage your stop words directly from the command line:
```bash
ai-rule-miner --show-stopwords
ai-rule-miner --add-stopword "foo"
ai-rule-miner --remove-stopword "foo"
```

## MCP Server Integration

To dynamically feed these learned rules back to your AI assistants (like Claude Desktop), add the MCP Server to your configuration (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ai-rule-miner": {
      "command": "ai-rule-miner-mcp"
    }
  }
}
```

The MCP Server provides:
- **Resource (`rules://global`)**: Dynamically aggregates your rules on demand.
- **Tools**: 
  - `analyze_recent_corrections(project_name)`
  - `get_stopwords()` / `add_stopword(word)` / `remove_stopword(word)`
  - `get_patterns()` / `add_pattern(pattern, weight, tag)`

This allows Claude to read, learn from, and dynamically update your preferences directly via chat.
