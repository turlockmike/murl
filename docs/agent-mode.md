# Output Format

murl is LLM-friendly by default — compact JSON to stdout, structured errors to stderr. Use `-v` for human-readable pretty-printing.

## stdout

- **Single results:** compact JSON (one line, no extra whitespace)
- **Lists:** NDJSON (one JSON object per line)

```bash
# Single tool call — compact JSON
murl http://localhost:3000/tools/echo -d message=hello
{"content":[{"type":"text","text":"hello"}]}

# List tools — one JSON object per line
murl http://localhost:3000/tools
{"name":"echo","description":"Echo a message",...}
{"name":"fetch","description":"Fetch a URL",...}
```

## stderr

Errors are structured JSON:

```json
{"error": "HTTPSTATUSERROR", "message": "401 Unauthorized", "code": 1}
```

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | General error (connection, timeout, server error) |
| `2` | Invalid arguments (malformed URL, invalid data format) |

## Verbose Mode

`-v` switches to human-friendly output: pretty-printed JSON and request/response debug info on stderr.

```bash
murl http://localhost:3000/tools -v
```

## Piping

Output is designed for standard Unix pipelines:

```bash
# Format with jq
murl http://localhost:3000/tools | jq .

# Extract fields
murl http://localhost:3000/tools | jq -r '.name'

# Handle errors programmatically
if ! result=$(murl http://localhost:3000/tools/invalid 2>/dev/null); then
  echo "failed"
fi
```
