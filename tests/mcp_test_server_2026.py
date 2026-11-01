"""MCP 2026-07-28 test server: handshake-less, `_meta`-enveloped, header-checked.

The sibling `mcp_test_server.py` speaks the 2025 era (`initialize` handshake,
plain JSON results). This one speaks the 2026-07-28 revision, so the two
together let a test prove murl against both revisions with no network, no Okta
and no AWS.

It is deliberately strict, because a permissive stub cannot prove a client
correct — every requirement below is one murl must satisfy, and each rejection
is a real error code a real server returns:

* `server/discover` is answered (the revision's replacement for `initialize`)
  and `initialize` is REFUSED with `-32601`, so a client that still handshakes
  fails here rather than silently working.
* Every request must carry the `_meta` envelope with BOTH
  `io.modelcontextprotocol/protocolVersion` and `…/clientCapabilities`;
  a missing key is `-32602`.
* `Mcp-Method` must be present and agree with the body; a named method
  (`tools/call`, `resources/read`, `prompts/get`) must also carry a matching
  `Mcp-Name`. Mismatch or omission is `-32020` (HeaderMismatch).
* Every result carries `resultType`, and cacheable results additionally carry
  `ttlMs` / `cacheScope`.

The request shapes it validates were captured from the real
`@modelcontextprotocol` Python client 2.0.0 rather than read off the spec, so a
murl request that differs from what the official client emits is a murl bug.

Run standalone: `TEST_PORT=8767 python tests/mcp_test_server_2026.py`
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "2026-07-28"

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC / MCP error codes used below.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020

SERVER_INFO = {"name": "mcp-test-server-2026", "version": "0.1.0"}

# Methods whose params name the target the call acts on. These require `Mcp-Name`.
NAMED_METHODS = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}

# Results that carry cache hints on this revision (SEP-2549).
CACHEABLE_METHODS = {
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "server/discover",
}

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the input message",
        "inputSchema": {
            "type": "object",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "properties": {"message": {"type": "string", "description": "Message to echo back"}},
            "required": ["message"],
        },
    },
    {
        "name": "weather",
        "description": "Get weather information for a city",
        "inputSchema": {
            "type": "object",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "metric": {"type": "boolean", "description": "Use metric units"},
            },
            "required": ["city"],
        },
    },
]


class MCP2026Handler(BaseHTTPRequestHandler):
    """Serves the 2026-07-28 revision over HTTP POST.

    `HTTP/1.1` (so responses are keep-alive capable, which is what a real client
    negotiates) requires a threading server: a single-threaded handler serves one
    connection at a time, so a client holding a keep-alive connection open blocks
    every subsequent request forever. Pair this class with `ThreadingHTTPServer`.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        try:
            length = int(self.headers.get("Content-Length", 0))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._send_error(None, -32700, "Parse error")
            return

        if request.get("jsonrpc") != "2.0":
            self._send_error(request.get("id"), -32600, "Invalid Request")
            return

        method = request.get("method")
        params = request.get("params") or {}
        request_id = request.get("id")

        # Notifications: acknowledge and drop. This revision has no
        # `notifications/initialized`, but a client may still send others.
        if request_id is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # A 2025-era client reaching this server is a client that never probed.
        # Refuse rather than serve it, so the test can tell the eras apart.
        if method == "initialize":
            self._send_error(
                request_id,
                METHOD_NOT_FOUND,
                "Method not found: initialize is not part of 2026-07-28; use server/discover",
            )
            return

        envelope_error = self._check_envelope(params)
        if envelope_error is not None:
            self._send_error(request_id, INVALID_PARAMS, envelope_error, {"envelope": envelope_error})
            return

        header_error = self._check_headers(method, params)
        if header_error is not None:
            self._send_error(request_id, HEADER_MISMATCH, header_error)
            return

        handlers = {
            "server/discover": lambda: {
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}},
            },
            "tools/list": lambda: {"tools": TOOLS},
            "tools/call": lambda: self._call_tool(params),
            "resources/list": lambda: {
                "resources": [
                    {"uri": "file:///path/to/file1.txt", "name": "file1.txt", "mimeType": "text/plain"},
                ]
            },
            "resources/read": lambda: {
                "contents": [
                    {
                        "uri": params.get("uri", ""),
                        "mimeType": "text/plain",
                        "text": "Mock file content",
                    }
                ]
            },
            "prompts/list": lambda: {"prompts": [{"name": "greeting", "description": "Generate a greeting"}]},
            "prompts/get": lambda: {
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": f"Hello {(params.get('arguments') or {}).get('name', 'World')}!",
                        },
                    }
                ]
            },
        }

        handler = handlers.get(method)
        if handler is None:
            self._send_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
            return

        self._send_result(request_id, method, handler())

    def _check_envelope(self, params: dict[str, Any]) -> str | None:
        """Both envelope keys are required on every request of this revision."""
        meta = params.get("_meta") or {}
        if META_PROTOCOL_VERSION not in meta:
            return f"{META_PROTOCOL_VERSION}: missing"
        if META_CLIENT_CAPABILITIES not in meta:
            return f"{META_CLIENT_CAPABILITIES}: missing"
        version = meta[META_PROTOCOL_VERSION]
        if version != PROTOCOL_VERSION:
            return f"{META_PROTOCOL_VERSION}: {version!r} is not {PROTOCOL_VERSION!r}"
        return None

    def _check_headers(self, method: str, params: dict[str, Any]) -> str | None:
        """`Mcp-Method` always; `Mcp-Name` on the methods that name a target."""
        header_method = self.headers.get("Mcp-Method")
        if header_method is None:
            return f"the body names method {method} but the required Mcp-Method header is absent"
        if header_method != method:
            return f"the body names method {method} but the Mcp-Method header names {header_method}"

        param_key = NAMED_METHODS.get(method)
        if param_key is None:
            return None
        expected = params.get(param_key)
        header_name = self.headers.get("Mcp-Name")
        if header_name is None:
            return f"{method} names {expected!r} but the required Mcp-Name header is absent"
        if header_name != expected:
            return f"{method} names {expected!r} but the Mcp-Name header names {header_name!r}"
        return None

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            return {"content": [{"type": "text", "text": arguments.get("message", "")}]}
        if name == "weather":
            city = arguments.get("city", "Unknown")
            return {"content": [{"type": "text", "text": f"Weather in {city}: 72°F"}]}
        if name == "input_required":
            # The server pauses for input (MRTR). "kind" picks a URL or a form
            # elicitation.
            url_req = {"method": "elicitation/create", "params": {
                "mode": "url", "message": "Authorize Google",
                "url": "https://auth.example.com/connect?r=2",
            }}
            if arguments.get("kind") == "badscheme":
                bad = {"method": "elicitation/create", "params": {
                    "mode": "url", "message": "Authorize", "url": "javascript:alert(1)",
                }}
                return {"resultType": "input_required", "inputRequests": {"r1": bad}, "requestState": "s1"}
            if arguments.get("kind") == "sampling":
                sampling = {"method": "sampling/createMessage", "params": {
                    "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
                    "maxTokens": 10,
                }}
                return {"resultType": "input_required",
                        "inputRequests": {"r1": url_req, "r2": sampling}, "requestState": "s1"}
            if arguments.get("kind") == "mixed":
                form = {"method": "elicitation/create", "params": {
                    "mode": "form", "message": "Your username",
                    "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
                }}
                return {"resultType": "input_required",
                        "inputRequests": {"r1": url_req, "r2": form}, "requestState": "s1"}
            if arguments.get("kind") == "form":
                req = {"method": "elicitation/create", "params": {
                    "mode": "form", "message": "Your username",
                    "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
                }}
            else:
                req = {"method": "elicitation/create", "params": {
                    "mode": "url", "message": "Authorize Google",
                    "url": "https://auth.example.com/connect?r=2",
                }}
            return {"resultType": "input_required", "inputRequests": {"r1": req}, "requestState": "s1"}
        # An unknown tool is a tool-level error, not a protocol fault — and it is
        # still a COMPLETE result, which is what distinguishes it from a server
        # pausing for input.
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def _send_result(self, request_id: Any, method: str, result: dict[str, Any]) -> None:
        result = dict(result)
        result.setdefault("resultType", "complete")
        result["_meta"] = {**result.get("_meta", {}), META_SERVER_INFO: SERVER_INFO}
        if method in CACHEABLE_METHODS:
            result.setdefault("ttlMs", 0)
            result.setdefault("cacheScope", "private")
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_error(
        self, request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        # A protocol-level rejection is an HTTP 400 carrying a JSON-RPC error,
        # matching what the reference server does.
        self._write({"jsonrpc": "2.0", "id": request_id, "error": error}, status=400)

    def _write(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        """Suppress default request logging."""


def run_server(port: int = 8767) -> None:
    server = ThreadingHTTPServer(("localhost", port), MCP2026Handler)
    print(f"MCP {PROTOCOL_VERSION} test server running on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
        server.shutdown()


if __name__ == "__main__":
    run_server(int(os.environ.get("TEST_PORT", 8767)))
