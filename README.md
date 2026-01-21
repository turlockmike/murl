# murl - MCP Curl

[![Tests](https://github.com/turlockmike/murl/actions/workflows/test.yml/badge.svg)](https://github.com/turlockmike/murl/actions/workflows/test.yml)

A curl-like CLI tool for interacting with Model Context Protocol (MCP) servers.

## What is MCP?

MCP (Model Context Protocol) is an open standard developed by Anthropic for AI models to access external data sources, tools, and services. It provides a universal way for large language models (LLMs) to interact with various resources securely and efficiently.

## Quick Start

### Try with Public Demo Server (No Setup Required)

Test murl immediately with a public MCP server:

```bash
# Install murl
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/master/install.sh | bash
# Or using pip: pip install mcp-curl

# List available tools on the public echo server
murl https://echo.mcp.inevitable.fyi/mcp/tools

# Call the echo tool
murl https://echo.mcp.inevitable.fyi/mcp/tools/echo -d message="Hello, World!"
```

**Public demo servers:**
- MCPlane Echo Server: `https://echo.mcp.inevitable.fyi/mcp` - Simple echo server for testing
- Microsoft Learn MCP: `https://learn.microsoft.com/api/mcp` - Documentation and learning tools

### Quick Local Setup with mcp-proxy

Get started with murl in minutes using a local MCP server:

```bash
# Step 1: Install murl
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/master/install.sh | bash
# Or using pip: pip install mcp-curl

# Step 2: Install mcp-proxy to expose MCP servers over HTTP
pip install mcp-proxy

# Step 3: Start a local time server example (in one terminal)
mcp-proxy --port 3000 uvx mcp-server-time

# Step 4: Test with murl (in another terminal)
# List available tools
murl http://localhost:3000/tools

# Call the get_current_time tool
murl http://localhost:3000/tools/get_current_time

# Call with a timezone argument
murl http://localhost:3000/tools/get_current_time -d timezone=America/New_York
```

**What's happening:**
- `mcp-proxy` wraps any stdio-based MCP server and exposes it over HTTP
- `uvx mcp-server-time` is a simple MCP server that provides time-related tools
- `murl` connects to the HTTP endpoint and makes MCP requests

**Try other MCP servers:**
```bash
# Filesystem server (access files)
mcp-proxy --port 3001 uvx mcp-server-filesystem /path/to/directory

# Sequential thinking server
mcp-proxy --port 3002 npx -y @modelcontextprotocol/server-sequential-thinking
```

## Installation

### Quick Install (Recommended)

Install murl with a single command:

```bash
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/master/install.sh | bash
```

This will automatically download and install murl from source.

### Using pip

Install murl from PyPI:

```bash
pip install mcp-curl
```

To upgrade to the latest version:

```bash
pip install --upgrade mcp-curl
```

### Upgrade

To upgrade murl to the latest version:

```bash
murl --upgrade
```

This command downloads and runs the installation script to update murl to the latest release from GitHub.

### From Source

```bash
git clone https://github.com/turlockmike/murl.git
cd murl
pip install -e .
```

## Usage

`murl` provides a curl-like interface for interacting with MCP servers over HTTP. It abstracts the JSON-RPC 2.0 protocol, making it easy to call MCP methods using intuitive REST-like paths.

### Basic Syntax

```bash
murl <url> [options]
```

Where `<url>` is the MCP server endpoint with a virtual path (e.g., `http://localhost:3000/tools`).

### Options

- `-d, --data <key=value>` - Add data to the request. Can be used multiple times.
- `-H, --header <key: value>` - Add custom HTTP headers (e.g., for authentication).
- `-v, --verbose` - Enable verbose output (prints request/response details to stderr).
- `--version` - Show detailed version information (includes Python version and installation path).
- `--upgrade` - Upgrade murl to the latest version from GitHub releases.
- `--help` - Show help message.

### Examples

#### List Available Tools

```bash
murl http://localhost:3000/tools
```

This sends a `tools/list` request to the MCP server.

#### Call a Tool with Arguments

```bash
murl http://localhost:3000/tools/echo -d message=hello
```

This sends a `tools/call` request with the tool name "echo" and arguments `{"message": "hello"}`.

#### Call a Tool with Multiple Arguments

```bash
murl http://localhost:3000/tools/weather -d city=Paris -d metric=true
```

Arguments are automatically type-coerced (strings, numbers, booleans).

#### Call a Tool with JSON Data

```bash
murl http://localhost:3000/tools/config -d '{"settings": {"theme": "dark"}}'
```

You can pass complex JSON objects directly.

#### List Available Resources

```bash
murl http://localhost:3000/resources
```

This sends a `resources/list` request.

#### Read a Resource

```bash
murl http://localhost:3000/resources/path/to/file
```

This sends a `resources/read` request with the file path. The path is automatically converted to a `file://` URI.

#### List Available Prompts

```bash
murl http://localhost:3000/prompts
```

This sends a `prompts/list` request.

#### Get a Prompt

```bash
murl http://localhost:3000/prompts/greeting -d name=Alice
```

This sends a `prompts/get` request with the prompt name "greeting" and arguments.

#### Add Authorization Headers

```bash
murl http://localhost:3000/tools -H "Authorization: Bearer token123"
```

Custom headers can be added for authentication or other purposes.

#### Verbose Mode

```bash
murl http://localhost:3000/tools -v
```

Verbose mode prints the JSON-RPC request payload and HTTP headers to stderr, useful for debugging.

### URL Mapping

`murl` automatically maps REST-like paths to MCP JSON-RPC methods:

| URL Path                  | MCP Method        | Parameters                                |
| ------------------------- | ----------------- | ----------------------------------------- |
| `/tools`                  | `tools/list`      | `{}`                                      |
| `/tools/<name>`           | `tools/call`      | `{name: "<name>", arguments: {...}}`      |
| `/resources`              | `resources/list`  | `{}`                                      |
| `/resources/<path>`       | `resources/read`  | `{uri: "file:///<path>"}` (three slashes) |
| `/prompts`                | `prompts/list`    | `{}`                                      |
| `/prompts/<name>`         | `prompts/get`     | `{name: "<name>", arguments: {...}}`      |

### Piping Output

`murl` outputs raw JSON to stdout, making it pipe-friendly:

```bash
# Use with jq to format output
murl http://localhost:3000/tools | jq .

# Extract specific fields
murl http://localhost:3000/tools | jq '.[0].name'
```

## Development

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/turlockmike/murl.git
cd murl

# Install in development mode with dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest
```

### Running Tests with Coverage

```bash
pytest --cov=murl --cov-report=html
```

## How It Works

`murl` works by:

1. **Parsing the URL** to extract the base endpoint and the MCP virtual path
2. **Mapping the virtual path** to the appropriate MCP JSON-RPC method
3. **Parsing data flags** (`-d`) into method parameters with type coercion
4. **Constructing a JSON-RPC 2.0 request** with the method and parameters
5. **Sending an HTTP POST request** to the base endpoint with the JSON-RPC payload
6. **Extracting the result** from the JSON-RPC response and printing it as JSON

## Using murl with MCP Servers

`murl` supports the Streamable HTTP transport protocol used by modern MCP servers. This allows murl to work with MCP servers that implement HTTP-based transport.

### Streamable HTTP Support

As of the latest version, murl includes comprehensive support for the MCP Streamable HTTP transport protocol:
- Sends `Accept: application/json, text/event-stream` header
- Handles both immediate JSON responses and Server-Sent Events (SSE) streams
- **Supports session-based SSE** for compatibility with mcp-proxy
- Automatically tries session-based SSE first, then falls back to regular HTTP POST
- Compatible with MCP servers implementing the Streamable HTTP specification

### Direct HTTP MCP Servers

murl works best with MCP servers that expose a direct HTTP JSON-RPC endpoint. For example, if you have a server running at `http://localhost:3000` that implements MCP over HTTP:

```bash
# List available tools
murl http://localhost:3000/tools

# Call a tool with arguments
murl http://localhost:3000/tools/my_tool -d param1=value1 -d param2=value2

# List resources
murl http://localhost:3000/resources

# Get a prompt
murl http://localhost:3000/prompts/my_prompt -d arg1=value
```

### Using murl with mcp-proxy

Many MCP servers are implemented as stdio (standard input/output) programs. To use these with murl, you can expose them via HTTP using [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy):

```bash
# Install mcp-proxy
pip install mcp-proxy

# Start mcp-proxy to expose a stdio MCP server on HTTP port 3000
mcp-proxy --port 3000 python my_mcp_server.py

# Or for a Node.js MCP server
mcp-proxy --port 3000 node path/to/mcp-server.js
```

Once mcp-proxy is running, you can use murl to interact with your stdio MCP server:

```bash
# List available tools
murl http://localhost:3000/tools

# Call a tool with arguments
murl http://localhost:3000/tools/my_tool -d param1=value1 -d param2=value2

# List resources
murl http://localhost:3000/resources

# Get a prompt
murl http://localhost:3000/prompts/my_prompt -d arg1=value
```

**How it works**: murl automatically detects mcp-proxy's session-based SSE architecture and handles it transparently:
1. Connects to the SSE endpoint to get a session ID
2. Posts the request to the session-specific endpoint
3. Reads the response from the SSE stream
4. Each murl invocation creates and closes its own ephemeral session

For more information about MCP transport protocols, see the [official MCP documentation](https://modelcontextprotocol.io/specification/basic/transports).

## Requirements

- Python 3.10 or higher
- `click` - For CLI argument parsing
- `mcp` - Model Context Protocol SDK

## License

MIT License - see LICENSE file for details

## Releasing

For maintainers: To create a new release, update the version in `pyproject.toml` and `murl/__init__.py`, then create and push a git tag:

```bash
git tag v0.2.1
git push origin v0.2.1
```

This will automatically trigger a GitHub Actions workflow that builds the package and creates a GitHub release with the artifacts.
