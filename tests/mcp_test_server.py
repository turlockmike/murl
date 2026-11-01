"""Real MCP-compatible HTTP JSON-RPC test server for integration testing.

Session-management behaviour is gated behind the ``MCP_TEST_SESSIONS`` env var
so existing tests (which don't set it) see the exact pre-session behaviour. When
enabled, the server:

  - assigns/echoes an ``Mcp-Session-Id`` on ``initialize`` (minting a new one
    only when the request arrives without a known session ID),
  - returns 404 ("session terminated") for an ``initialize`` carrying a session
    ID it never minted (models AgentCore reaping an idle runtime session),
  - exposes a ``paginate`` tool (cursor-based) and a ``session_info`` tool, and
  - appends one JSON line per lifecycle event to ``MCP_TEST_STATE_FILE`` so a
    test can assert how many sessions were minted/reused.
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List

# Cross-request state for the (single-threaded) server process.
_STATE: Dict[str, Any] = {"counter": 0, "live": set()}
_STATE_LOCK = threading.Lock()

MCP_SESSION_ID_HEADER = "Mcp-Session-Id"


def _sessions_enabled() -> bool:
    return os.environ.get("MCP_TEST_SESSIONS") not in (None, "", "0")


def _proxy_rewrite_enabled() -> bool:
    """Emulate a hosting proxy that rewrites runtime JSON-RPC errors.

    Amazon Bedrock AgentCore collapses a runtime-side
    JSON-RPC error into a generic 400, so the `-32601` a 2025-era server sends
    for `server/discover` never reaches the client — it arrives as
    `-32010 "Received error (400) from runtime"` instead.

    A client that decides the protocol era from the error CODE therefore cannot
    reach these servers at all. Set `MCP_TEST_PROXY_REWRITE=1` to reproduce that
    in a local test; the server stays a plain 2025-era server in every other
    respect, so `initialize` still succeeds and the fallback is what gets tested.
    """
    return os.environ.get("MCP_TEST_PROXY_REWRITE") not in (None, "", "0")


def _record_event(event: Dict[str, Any]) -> None:
    """Append a lifecycle event as one JSON line to MCP_TEST_STATE_FILE."""
    path = os.environ.get("MCP_TEST_STATE_FILE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


class RpcError(Exception):
    """A handler's JSON-RPC error reply: code, message and optional data."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _mint_session_id() -> str:
    with _STATE_LOCK:
        _STATE["counter"] += 1
        sid = f"sess-{_STATE['counter']}"
        _STATE["live"].add(sid)
        return sid


def _is_live(session_id: str) -> bool:
    with _STATE_LOCK:
        return session_id in _STATE["live"]


class MCPJSONRPCHandler(BaseHTTPRequestHandler):
    """Handler for MCP JSON-RPC requests over HTTP POST."""

    def do_POST(self):
        """Handle POST requests with JSON-RPC payloads."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            request = json.loads(post_data.decode('utf-8'))

            # Validate JSON-RPC request
            if request.get('jsonrpc') != '2.0':
                self.send_error_response(request.get('id'), -32600, "Invalid Request")
                return

            method = request.get('method')
            params = request.get('params', {})
            request_id = request.get('id')

            # Handle notifications (no response expected)
            if request_id is None:
                if method == 'notifications/initialized':
                    # Just acknowledge the notification
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    # Unknown notification - just acknowledge
                    self.send_response(202)
                    self.end_headers()
                    return

            extra_headers: Dict[str, str] = {}

            # Session management (gated): reject a stale/unknown session ID with a
            # 404 so the client starts a fresh session (MCP 2025-11-25 §Session
            # Management). Checked before method dispatch, not only on
            # `initialize`: an unknown session is unknown whatever the request
            # carrying it, and a client that probes with `server/discover` first
            # would otherwise get "method not found" and never learn its cached
            # session is dead.
            if _sessions_enabled():
                incoming = self.headers.get(MCP_SESSION_ID_HEADER)
                if incoming and not _is_live(incoming):
                    _record_event({"event": "initialize_rejected", "session_id": incoming})
                    self.send_response(404)
                    self.end_headers()
                    return

            # Route to appropriate handler
            if method == 'initialize':
                if _sessions_enabled():
                    incoming = self.headers.get(MCP_SESSION_ID_HEADER)
                    session_id = incoming if incoming else _mint_session_id()
                    extra_headers[MCP_SESSION_ID_HEADER] = session_id
                    _record_event({
                        "event": "initialize",
                        "session_id": session_id,
                        "minted": incoming is None,
                    })
                result = self.handle_initialize(params)
            elif method == 'tools/list':
                result = self.handle_tools_list()
            elif method == 'tools/call':
                try:
                    result = self.handle_tools_call(params)
                except RpcError as e:
                    self.send_error_response(request_id, e.code, e.message, e.data)
                    return
            elif method == 'resources/list':
                result = self.handle_resources_list()
            elif method == 'resources/read':
                result = self.handle_resources_read(params)
            elif method == 'prompts/list':
                result = self.handle_prompts_list()
            elif method == 'prompts/get':
                result = self.handle_prompts_get(params)
            else:
                if _proxy_rewrite_enabled():
                    # What a proxied runtime actually returns: the -32601 is gone,
                    # replaced by a generic code and message that say nothing
                    # about which method was missing.
                    self.send_error_response(
                        request_id,
                        -32010,
                        "Received error (400) from runtime. Please check your "
                        "CloudWatch logs for more information.",
                    )
                    return
                self.send_error_response(request_id, -32601, f"Method not found: {method}")
                return

            # Send successful response
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            for key, value in extra_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))

        except json.JSONDecodeError:
            self.send_error_response(None, -32700, "Parse error")
        except Exception as e:
            self.send_error_response(None, -32603, f"Internal error: {str(e)}")

    def do_GET(self):
        """No server-initiated SSE stream here.

        405 lets the SDK's optional GET-stream attempt fail cleanly (it retries
        briefly, then gives up) without affecting the POST request/response.
        """
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        """Session termination (the terminate_on_close=True path)."""
        if _sessions_enabled():
            sid = self.headers.get(MCP_SESSION_ID_HEADER)
            if sid:
                with _STATE_LOCK:
                    _STATE["live"].discard(sid)
            _record_event({"event": "delete", "session_id": sid})
        self.send_response(200)
        self.end_headers()

    def send_error_response(self, request_id: Any, code: int, message: str, data: Any = None):
        """Send a JSON-RPC error response."""
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message
            }
        }
        if data is not None:
            response["error"]["data"] = data

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request per MCP spec."""
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {}
            },
            "serverInfo": {
                "name": "mcp-test-server",
                "version": "0.1.0"
            }
        }

    def handle_tools_list(self) -> Dict[str, Any]:
        """Handle tools/list request."""
        tools = [
            {
                "name": "echo",
                "description": "Echo back the input message",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Message to echo back"
                        }
                    },
                    "required": ["message"]
                }
            },
            {
                "name": "weather",
                "description": "Get weather information for a city",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "City name"
                        },
                        "metric": {
                            "type": "boolean",
                            "description": "Use metric units"
                        }
                    },
                    "required": ["city"]
                }
            },
        ]
        if _sessions_enabled():
            tools.append({
                "name": "paginate",
                "description": "Return a cursor-paginated list (for session tests)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"cursor": {"type": "string"}},
                },
            })
            tools.append({
                "name": "session_info",
                "description": "Report the Mcp-Session-Id seen on this request",
                "inputSchema": {"type": "object", "properties": {}},
            })
        return {"tools": tools}

    def handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request."""
        name = params.get('name')
        arguments = params.get('arguments', {})

        if name == "echo":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": arguments.get("message", "")
                    }
                ]
            }
        elif name == "weather":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Weather in {arguments.get('city', 'Unknown')}: 72°F"
                    }
                ]
            }
        elif name == "paginate":
            # Cursor-based pages of 2 items each. The cursor is the next page
            # index; absent cursor means page 0. hasMore is dropped (and no
            # nextCursor) on the final page so murl's follow loop terminates.
            pages = int(os.environ.get("MCP_TEST_PAGES", "3"))
            cursor = arguments.get("cursor")
            idx = 0 if cursor is None else int(cursor)
            body: Dict[str, Any] = {
                "items": [f"item-{idx * 2}", f"item-{idx * 2 + 1}"],
                "hasMore": idx < pages - 1,
            }
            if body["hasMore"]:
                body["nextCursor"] = str(idx + 1)
            return {"content": [{"type": "text", "text": json.dumps(body)}]}
        elif name == "fail":
            # A tool execution error (MCP tools §Error Handling). "second" adds
            # a second text block.
            content = [{"type": "text", "text": arguments.get("message", "tool failed")}]
            if arguments.get("second"):
                content.append({"type": "text", "text": arguments["second"]})
            return {"content": content, "isError": True}
        elif name == "consent":
            # Third-party authorization needed, 2025-11-25 form: -32042.
            # "url" and "message" override the defaults; "nodata" sends no data.
            if arguments.get("nodata"):
                raise RpcError(-32042, "Authorization required")
            raise RpcError(-32042, arguments.get("message", "Authorize Google to continue"),
                           {"elicitations": [{
                               "mode": "url", "message": "Authorize Google",
                               "url": arguments.get("url", "https://auth.example.com/connect?r=1"),
                               "elicitationId": "e1",
                           }]})
        elif name == "fail_page2":
            # Page 1 has more; page 2 is a tool execution error.
            if arguments.get("cursor") is None:
                body = {"items": ["a"], "hasMore": True, "nextCursor": "1"}
                return {"content": [{"type": "text", "text": json.dumps(body)}]}
            return {"content": [{"type": "text", "text": "quota exceeded"}], "isError": True}
        elif name == "session_info":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"session_id": self.headers.get(MCP_SESSION_ID_HEADER)}),
                    }
                ]
            }
        else:
            raise ValueError(f"Unknown tool: {name}")

    def handle_resources_list(self) -> Dict[str, Any]:
        """Handle resources/list request."""
        return {
            "resources": [
                {
                    "uri": "file:///path/to/file1.txt",
                    "name": "file1.txt",
                    "mimeType": "text/plain"
                },
                {
                    "uri": "file:///path/to/file2.txt",
                    "name": "file2.txt",
                    "mimeType": "text/plain"
                },
            ]
        }

    def handle_resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle resources/read request."""
        uri = params.get('uri', '')
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/plain",
                    "text": "Mock file content"
                }
            ]
        }

    def handle_prompts_list(self) -> Dict[str, Any]:
        """Handle prompts/list request."""
        return {
            "prompts": [
                {
                    "name": "greeting",
                    "description": "Generate a greeting"
                },
                {
                    "name": "summary",
                    "description": "Generate a summary"
                },
            ]
        }

    def handle_prompts_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle prompts/get request."""
        name = params.get('name', '')
        arguments = params.get('arguments', {})
        user_name = arguments.get('name', 'World')

        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Hello {user_name}!"
                    }
                }
            ]
        }

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def run_server(port: int = 8765):
    """Run the MCP test server."""
    server = HTTPServer(('localhost', port), MCPJSONRPCHandler)
    print(f"MCP JSON-RPC test server running on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
        server.shutdown()


if __name__ == "__main__":
    port = int(os.environ.get('TEST_PORT', 8765))
    run_server(port)
