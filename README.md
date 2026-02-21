# murl - MCP Curl

[![Tests](https://github.com/turlockmike/murl/actions/workflows/test.yml/badge.svg)](https://github.com/turlockmike/murl/actions/workflows/test.yml)

A curl-like CLI for [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers. Query tools, resources, and prompts using simple REST-like URLs.

**LLM-friendly:** compact JSON output, NDJSON streaming, structured errors to stderr, semantic exit codes. Built for agents to call from shell.

<p align="center">
  <img src="images/logo.png" alt="murl logo" width="400">
</p>

## Installation

### Homebrew (Recommended)

```bash
brew install turlockmike/murl/murl
```

### pip

```bash
pip install mcp-curl
```

### Shell script

```bash
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/master/install.sh | bash
```

To upgrade: `brew upgrade turlockmike/murl/murl` or `murl --upgrade`

## Quick Start

```bash
# List tools on a public MCP server
murl https://remote.mcpservers.org/fetch/mcp/tools

# Call a tool with arguments
murl https://remote.mcpservers.org/fetch/mcp/tools/fetch -d url=https://example.com

# Use a local server via mcp-proxy
mcp-proxy --port 3000 uvx mcp-server-time
murl http://localhost:3000/tools/get_current_time -d timezone=America/New_York
```

**Public demo servers:**
- `https://remote.mcpservers.org/fetch/mcp` — fetch web content
- `https://mcp.deepwiki.com/mcp` — GitHub repository docs

## Usage

```bash
murl <url> [options]
```

### URL Mapping

| URL Path | MCP Method |
|---|---|
| `/tools` | `tools/list` |
| `/tools/<name>` | `tools/call` |
| `/resources` | `resources/list` |
| `/resources/<path>` | `resources/read` |
| `/prompts` | `prompts/list` |
| `/prompts/<name>` | `prompts/get` |

### Examples

```bash
# List tools
murl http://localhost:3000/tools

# Call a tool
murl http://localhost:3000/tools/echo -d message=hello

# Multiple arguments (auto type-coerced)
murl http://localhost:3000/tools/weather -d city=Paris -d metric=true

# JSON data
murl http://localhost:3000/tools/config -d '{"settings": {"theme": "dark"}}'

# Custom headers
murl http://localhost:3000/tools -H "Authorization: Bearer token123"

# Verbose mode (prints request/response to stderr)
murl http://localhost:3000/tools -v

# Pipe to jq
murl http://localhost:3000/tools | jq '.[0].name'
```

### Options

| Flag | Description |
|---|---|
| `-d, --data` | Add key=value or JSON data (repeatable) |
| `-H, --header` | Add HTTP header (repeatable) |
| `-v, --verbose` | Pretty-print output, show request debug info |
| `--login` | Force OAuth re-authentication |
| `--no-auth` | Skip all authentication |
| `--version` | Show version info |
| `--upgrade` | Upgrade to latest version |

### OAuth

murl supports OAuth 2.0 with Dynamic Client Registration (RFC 7591) and PKCE. Tokens are cached automatically.

```bash
# First call triggers browser-based OAuth flow
murl https://example.com/mcp/tools

# Skip auth for public servers
murl https://example.com/mcp/tools --no-auth

# Force re-authentication
murl https://example.com/mcp/tools --login
```

## Documentation

- [Output & Exit Codes](docs/agent-mode.md) — NDJSON format, structured errors, exit codes
- [MCP Server Setup](docs/mcp-servers.md) — mcp-proxy, Streamable HTTP, local servers
- [Contributing](docs/contributing.md) — development setup, testing, releasing

## Requirements

- Python 3.10+

## License

MIT
