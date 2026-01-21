# murl - MCP Curl

[![Tests](https://github.com/turlockmike/murl/actions/workflows/test.yml/badge.svg)](https://github.com/turlockmike/murl/actions/workflows/test.yml)

A curl-like CLI tool for interacting with Model Context Protocol (MCP) servers.

## What is MCP?

MCP (Model Context Protocol) is an open standard developed by Anthropic for AI models to access external data sources, tools, and services. It provides a universal way for large language models (LLMs) to interact with various resources securely and efficiently.

## Installation

### Quick Install (Recommended)

Install murl with a single command:

```bash
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/main/install.sh | bash
```

This will automatically install murl using pip and ensure it's available in your PATH.

### Using pip

```bash
pip install murl
```

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
- `--version` - Show version information.
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
murl http://localhost:3000/resources/read -d uri=file:///path/to/file
```

This sends a `resources/read` request with the specified URI.

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
| `/resources/read`         | `resources/read`  | `{...}` (expects `uri` in data)           |
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

## Using murl with mcp-proxy

Many MCP servers are implemented as stdio (standard input/output) programs that communicate via JSON-RPC over stdin/stdout. To use `murl` with these servers, you need to convert them to HTTP endpoints. The [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) tool bridges stdio MCP servers to HTTP transport, making them accessible via `murl`.

### Installing mcp-proxy

```bash
pip install mcp-proxy
```

### Converting a stdio MCP Server to HTTP

If you have a stdio MCP server (e.g., a Python script, Node.js app, or any program that communicates via stdin/stdout), you can expose it as an HTTP server using mcp-proxy:

```bash
# Start mcp-proxy to expose a stdio MCP server on HTTP port 3000
mcp-proxy --sse-port 3000 your-stdio-mcp-server

# Or for a Node.js MCP server
mcp-proxy --sse-port 3000 node path/to/mcp-server.js

# Or for a Python MCP server
mcp-proxy --sse-port 3000 python path/to/mcp_server.py
```

This starts an HTTP server on `http://localhost:3000` that forwards requests to your stdio MCP server.

### Using murl with the Proxied Server

Once mcp-proxy is running, you can use `murl` to interact with your stdio MCP server via HTTP:

```bash
# List available tools from the proxied server
murl http://localhost:3000/tools

# Call a tool with arguments
murl http://localhost:3000/tools/my_tool -d param1=value1 -d param2=value2

# List resources
murl http://localhost:3000/resources

# Get a prompt
murl http://localhost:3000/prompts/my_prompt -d arg1=value
```

### Example: Complete Workflow

Here's a complete example of setting up and using a stdio MCP server with murl:

1. **Start your stdio MCP server via mcp-proxy:**
   ```bash
   mcp-proxy --sse-port 3000 python my_mcp_server.py
   ```

2. **In another terminal, use murl to interact with it:**
   ```bash
   # Discover available tools
   murl http://localhost:3000/tools | jq '.[] | {name, description}'
   
   # Call a specific tool
   murl http://localhost:3000/tools/process_data -d input="Hello World"
   
   # List available resources
   murl http://localhost:3000/resources | jq .
   ```

### Authentication with mcp-proxy

If your stdio MCP server requires authentication, mcp-proxy can handle it:

```bash
# Set up authentication via environment variable
export API_ACCESS_TOKEN=your-secret-token
mcp-proxy --sse-port 3000 your-mcp-server

# Then use murl with the auth header
murl http://localhost:3000/tools -H "Authorization: Bearer your-secret-token"
```

### Benefits of Using mcp-proxy with murl

- **Universal Access**: Convert any stdio MCP server to HTTP without modifying the server code
- **Testing**: Easily test MCP servers during development using familiar curl-like syntax
- **Integration**: Enable MCP servers to be accessed by HTTP-based clients and tools
- **Remote Access**: Expose local MCP servers for remote access (with appropriate security measures)

For more information about mcp-proxy, visit the [official repository](https://github.com/sparfenyuk/mcp-proxy).

## Requirements

- Python 3.10 or higher
- `click` - For CLI argument parsing
- `requests` - For HTTP requests

## License

MIT License - see LICENSE file for details

## Publishing

For maintainers: See [PUBLISHING.md](PUBLISHING.md) for instructions on publishing new versions to PyPI.
