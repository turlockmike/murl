# Agent Mode

murl implements [POSIX Agent Standard (Level 2)](https://github.com/turlockmike/posix-agent-standard) for AI agent compatibility. Use `--agent` to enable.

## Behavior

- **Pure JSON output:** Compact JSON to stdout (no pretty-printing)
- **JSON Lines (NDJSON):** List operations output one JSON object per line
- **Structured errors:** JSON error objects to stderr with error codes
- **Non-interactive:** No prompts or progress indicators

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | General error (connection, timeout, server error) |
| `2` | Invalid arguments (malformed URL, invalid data format) |
| `100` | MCP server error (reported via JSON `code` field, not exit code) |

## Examples

```bash
# Agent-optimized help
murl --agent --help

# List tools (NDJSON — one JSON object per line)
murl --agent http://localhost:3000/tools

# Call a tool (compact JSON)
murl --agent http://localhost:3000/tools/echo -d message=hello

# Process with jq
murl --agent http://localhost:3000/tools | jq -c '.'

# Handle errors programmatically
if ! result=$(murl --agent http://localhost:3000/tools/invalid 2>&1); then
  echo "Error: $result" | jq -r '.message'
fi
```

## Human Mode vs Agent Mode

| Feature | Human Mode | Agent Mode (`--agent`) |
|---------|-----------|------------------------|
| JSON Output | Pretty-printed (indented) | Compact (no spaces) |
| List Output | JSON array | JSON Lines (NDJSON) |
| Error Output | Friendly message to stderr | Structured JSON to stderr |
| Exit Codes | 0, 1, or 2 | Semantic (0, 1, 2) |
