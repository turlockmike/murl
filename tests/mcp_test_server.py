"""Real MCP-compatible HTTP JSON-RPC test server for integration testing."""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List


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
            
            # Route to appropriate handler
            if method == 'initialize':
                result = self.handle_initialize(params)
            elif method == 'tools/list':
                result = self.handle_tools_list()
            elif method == 'tools/call':
                result = self.handle_tools_call(params)
            elif method == 'resources/list':
                result = self.handle_resources_list()
            elif method == 'resources/read':
                result = self.handle_resources_read(params)
            elif method == 'prompts/list':
                result = self.handle_prompts_list()
            elif method == 'prompts/get':
                result = self.handle_prompts_get(params)
            else:
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
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except json.JSONDecodeError:
            self.send_error_response(None, -32700, "Parse error")
        except Exception as e:
            self.send_error_response(None, -32603, f"Internal error: {str(e)}")
    
    def send_error_response(self, request_id: Any, code: int, message: str):
        """Send a JSON-RPC error response."""
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message
            }
        }
        
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
    
    def handle_tools_list(self) -> List[Dict[str, Any]]:
        """Handle tools/list request."""
        return [
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
    
    def handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request."""
        name = params.get('name')
        arguments = params.get('arguments', {})
        
        if name == "echo":
            return {"message": arguments.get("message", "")}
        elif name == "weather":
            return {
                "city": arguments.get("city", "Unknown"),
                "temperature": 72,
                "metric": arguments.get("metric", False)
            }
        else:
            raise ValueError(f"Unknown tool: {name}")
    
    def handle_resources_list(self) -> List[Dict[str, Any]]:
        """Handle resources/list request."""
        return [
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
    
    def handle_resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle resources/read request."""
        uri = params.get('uri', '')
        return {
            "uri": uri,
            "content": "Mock file content"
        }
    
    def handle_prompts_list(self) -> List[Dict[str, Any]]:
        """Handle prompts/list request."""
        return [
            {
                "name": "greeting",
                "description": "Generate a greeting"
            },
            {
                "name": "summary",
                "description": "Generate a summary"
            },
        ]
    
    def handle_prompts_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle prompts/get request."""
        name = params.get('name', '')
        arguments = params.get('arguments', {})
        user_name = arguments.get('name', 'World')
        
        return {
            "name": name,
            "prompt": f"Hello {user_name}!"
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
    import os
    port = int(os.environ.get('TEST_PORT', 8765))
    run_server(port)

