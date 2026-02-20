# MCP Server Setup

murl works with any MCP server that exposes an HTTP endpoint, including servers using the Streamable HTTP transport protocol.

## Direct HTTP Servers

If your MCP server already exposes an HTTP JSON-RPC endpoint:

```bash
murl http://localhost:3000/tools
murl http://localhost:3000/tools/my_tool -d param1=value1 -d param2=value2
murl http://localhost:3000/resources
murl http://localhost:3000/prompts/my_prompt -d arg1=value
```

## Using mcp-proxy for stdio Servers

Many MCP servers use stdio transport. Use [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) to expose them over HTTP:

```bash
# Install
pip install mcp-proxy

# Start a proxy for any stdio MCP server
mcp-proxy --port 3000 python my_mcp_server.py
mcp-proxy --port 3000 node path/to/mcp-server.js
mcp-proxy --port 3000 uvx mcp-server-time

# Then use murl normally
murl http://localhost:3000/tools
```

### How mcp-proxy works with murl

murl automatically detects mcp-proxy's session-based SSE architecture:

1. Connects to the SSE endpoint to get a session ID
2. Posts the request to the session-specific endpoint
3. Reads the response from the SSE stream
4. Each invocation creates and closes its own ephemeral session

### Example servers

```bash
# Time server
mcp-proxy --port 3000 uvx mcp-server-time

# Filesystem server
mcp-proxy --port 3001 uvx mcp-server-filesystem /path/to/directory

# Sequential thinking server
mcp-proxy --port 3002 npx -y @modelcontextprotocol/server-sequential-thinking
```

## Streamable HTTP

murl includes full support for the MCP Streamable HTTP transport:

- Sends `Accept: application/json, text/event-stream` header
- Handles both immediate JSON responses and SSE streams
- Supports session-based SSE for mcp-proxy compatibility
- Tries session-based SSE first, then falls back to regular HTTP POST

For more details, see the [MCP transport specification](https://modelcontextprotocol.io/specification/basic/transports).
