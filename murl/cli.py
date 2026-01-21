"""CLI entry point for murl."""

import json
import re
import sys
import time
from typing import Dict, Any, Tuple, Optional

import click
import requests
from murl import __version__


# Constants for session-based SSE
SSE_CONNECTION_TIMEOUT = 30  # Timeout for establishing SSE connection
SSE_POST_TIMEOUT = 10  # Timeout for POST request to session endpoint
SSE_READ_TIMEOUT = 10  # Timeout for reading response from SSE stream
SSE_LOG_TRUNCATE_LENGTH = 100  # Max characters to show in verbose SSE logs


def parse_url(full_url: str) -> Tuple[str, str]:
    """Parse the full URL into base URL and virtual path.
    
    Args:
        full_url: The complete URL including the MCP path
        
    Returns:
        Tuple of (base_url, virtual_path)
        
    Raises:
        ValueError: If the URL doesn't contain a valid MCP path
    """
    # Regex to find the start of MCP segments
    pattern = r'/(tools|resources|prompts)(\/.*)?$'
    match = re.search(pattern, full_url)
    
    if not match:
        raise ValueError(
            "Invalid MCP URL. Must contain /tools, /resources, or /prompts"
        )
    
    virtual_path = match.group(0)  # e.g., "/tools/weather"
    base_url = full_url[:match.start()]  # e.g., "https://api.com/mcp"
    
    return base_url, virtual_path


def parse_data_value(value: str) -> Any:
    """Parse a data value and coerce types.
    
    Args:
        value: The string value to parse
        
    Returns:
        The parsed value (bool, int, float, or string)
    """
    # Try to parse as boolean
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False
    
    # Try to parse as number
    try:
        # Try integer first
        if '.' not in value:
            return int(value)
        # Try float
        return float(value)
    except ValueError:
        pass
    
    # Return as string
    return value


def parse_data_flags(data_flags: Tuple[str, ...]) -> Dict[str, Any]:
    """Parse -d/--data flags into a dictionary.
    
    Args:
        data_flags: Tuple of data flag values
        
    Returns:
        Dictionary of parsed key-value pairs
        
    Note:
        JSON objects (starting with '{') are merged into the result.
        JSON arrays (starting with '[') are not supported as they don't
        represent key-value pairs needed for MCP arguments.
    """
    result = {}
    
    for data in data_flags:
        # Check if it's a JSON object
        stripped = data.strip()
        if stripped.startswith('{'):
            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError(f"JSON in -d flag must be an object, not {type(parsed).__name__}")
                result.update(parsed)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON in -d flag: {data}")
        elif stripped.startswith('['):
            raise ValueError(f"JSON arrays are not supported in -d flag. Use key=value or JSON objects.")
        else:
            # Parse key=value format
            if '=' not in data:
                raise ValueError(f"Invalid data format: {data}. Expected key=value or JSON")
            
            key, value = data.split('=', 1)
            result[key] = parse_data_value(value)
    
    return result


def map_virtual_path_to_method(virtual_path: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Map a virtual path to an MCP JSON-RPC method and params.
    
    Args:
        virtual_path: The MCP path (e.g., "/tools", "/tools/weather")
        data: The parsed data dictionary
        
    Returns:
        Tuple of (method, params)
    """
    # Remove leading slash and split
    parts = virtual_path.lstrip('/').split('/')
    
    # Check for empty path
    if not parts or parts == ['']:
        raise ValueError("Invalid virtual path: empty path")
    
    category = parts[0]  # tools, resources, or prompts
    
    if category == 'tools':
        if len(parts) == 1:
            # /tools -> tools/list
            return 'tools/list', {}
        else:
            # /tools/<name> -> tools/call
            tool_name = parts[1]
            return 'tools/call', {
                'name': tool_name,
                'arguments': data
            }
    
    elif category == 'resources':
        if len(parts) == 1:
            # /resources -> resources/list
            return 'resources/list', {}
        else:
            # /resources/<path> -> resources/read with file:// URI
            # Join all parts after 'resources' to form the file path
            # Prepend '/' to make it an absolute path: /resources/path/to/file -> file:///path/to/file
            file_path = '/'.join(parts[1:])
            # Handle empty path case (e.g., /resources/ with trailing slash)
            if not file_path or file_path == '':
                raise ValueError("Invalid resources path: path cannot be empty after /resources/")
            # Ensure absolute path starts with '/'
            if not file_path.startswith('/'):
                file_path = '/' + file_path
            uri = f'file://{file_path}'
            # Merge with any additional data parameters passed via -d flags
            return 'resources/read', {'uri': uri, **data}
    
    elif category == 'prompts':
        if len(parts) == 1:
            # /prompts -> prompts/list
            return 'prompts/list', {}
        else:
            # /prompts/<name> -> prompts/get
            prompt_name = parts[1]
            return 'prompts/get', {
                'name': prompt_name,
                'arguments': data
            }
    
    else:
        raise ValueError(f"Invalid MCP category: {category}")


def create_jsonrpc_request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a JSON-RPC 2.0 request envelope.
    
    Args:
        method: The MCP method name
        params: The parameters object
        
    Returns:
        JSON-RPC request dictionary
    """
    return {
        'jsonrpc': '2.0',
        'id': 1,
        'method': method,
        'params': params
    }


def parse_headers(header_flags: Tuple[str, ...]) -> Dict[str, str]:
    """Parse -H/--header flags into a dictionary.
    
    Args:
        header_flags: Tuple of header flag values
        
    Returns:
        Dictionary of headers
    """
    headers = {}
    
    for header in header_flags:
        if ':' not in header:
            raise ValueError(f"Invalid header format: {header}. Expected 'Key: Value'")
        
        key, value = header.split(':', 1)
        headers[key.strip()] = value.strip()
    
    return headers


def perform_initialization_handshake(
    session_endpoint: str,
    lines_iter,
    verbose: bool
) -> bool:
    """Perform MCP initialization handshake.
    
    This sends the initialize request and notifications/initialized as per
    MCP specification before any other requests can be made.
    
    Args:
        session_endpoint: The session endpoint to send requests to
        lines_iter: Iterator for reading SSE stream responses
        verbose: Whether to print verbose output
        
    Returns:
        True if initialization successful, False otherwise
    """
    try:
        # Create initialize request per MCP spec
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "murl",
                    "version": __version__
                }
            }
        }
        
        if verbose:
            click.echo("=== Performing MCP Initialization ===", err=True)
            click.echo(json.dumps(init_request, indent=2), err=True)
        
        # Send initialize request
        init_response = requests.post(
            session_endpoint,
            json=init_request,
            headers={'Content-Type': 'application/json'},
            timeout=SSE_POST_TIMEOUT
        )
        
        if init_response.status_code not in (200, 202):
            if verbose:
                click.echo(f"Initialize request failed: {init_response.status_code}", err=True)
            return False
        
        # Read initialize response from SSE stream
        start_time = time.time()
        init_response_data = None
        
        for line in lines_iter:
            if time.time() - start_time > SSE_READ_TIMEOUT:
                if verbose:
                    click.echo("Timeout waiting for initialize response", err=True)
                return False
            
            if not line:
                continue
            
            if line.startswith('data: '):
                data_str = line[6:]
                try:
                    message = json.loads(data_str)
                    if isinstance(message, dict) and message.get('id') == 0:
                        init_response_data = message
                        if verbose:
                            click.echo("Received initialize response", err=True)
                        break
                except json.JSONDecodeError:
                    continue
        
        if not init_response_data:
            if verbose:
                click.echo("No initialize response received", err=True)
            return False
        
        # Check for error in initialize response
        if 'error' in init_response_data:
            if verbose:
                click.echo(f"Initialize error: {init_response_data['error']}", err=True)
            return False
        
        # Send initialized notification (no response expected)
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        
        if verbose:
            click.echo("Sending initialized notification", err=True)
        
        notif_response = requests.post(
            session_endpoint,
            json=initialized_notification,
            headers={'Content-Type': 'application/json'},
            timeout=SSE_POST_TIMEOUT
        )
        
        if notif_response.status_code not in (200, 202):
            if verbose:
                click.echo(f"Initialized notification failed: {notif_response.status_code}", err=True)
            return False
        
        if verbose:
            click.echo("MCP initialization complete", err=True)
        
        return True
        
    except Exception as e:
        if verbose:
            click.echo(f"Initialization error: {e}", err=True)
        return False


def try_session_based_sse_request(
    base_url: str,
    jsonrpc_request: Dict[str, Any],
    headers: Dict[str, str],
    verbose: bool
) -> Optional[Dict[str, Any]]:
    """Try to make a session-based SSE request (for mcp-proxy compatibility).
    
    This handles the mcp-proxy session-based SSE workflow:
    1. Connect to SSE endpoint to get session ID
    2. Perform MCP initialization handshake
    3. POST request to session-specific endpoint
    4. Read response from SSE stream
    
    Args:
        base_url: The base URL of the server
        jsonrpc_request: The JSON-RPC request to send
        headers: HTTP headers
        verbose: Whether to print verbose output
        
    Returns:
        The response data if successful, None if session-based SSE not supported
    """
    try:
        # Try to connect to SSE endpoint to establish session
        sse_url = f"{base_url}/sse"
        
        if verbose:
            click.echo("=== Attempting Session-Based SSE ===", err=True)
            click.echo(f"Connecting to: {sse_url}", err=True)
        
        # Connect to SSE endpoint with streaming - keep connection alive
        sse_response = requests.get(
            sse_url,
            headers={'Accept': 'text/event-stream'},
            stream=True,
            timeout=SSE_CONNECTION_TIMEOUT
        )
        
        if sse_response.status_code != 200:
            return None
        
        # Parse SSE stream to get session endpoint
        session_endpoint = None
        lines_iter = sse_response.iter_lines(decode_unicode=True)
        
        # Read initial lines to get session endpoint
        for line in lines_iter:
            if not line:
                continue
            
            # Look for event: endpoint line followed by data: line
            if line.startswith('event: endpoint'):
                continue
            elif line.startswith('data: '):
                endpoint_path = line[6:].strip()  # Remove 'data: ' prefix
                if endpoint_path.startswith('/'):
                    session_endpoint = f"{base_url}{endpoint_path}"
                    break
        
        if not session_endpoint:
            if verbose:
                click.echo("No session endpoint found in SSE stream", err=True)
            sse_response.close()
            return None
        
        if verbose:
            click.echo(f"Got session endpoint: {session_endpoint}", err=True)
        
        # Perform MCP initialization handshake
        if not perform_initialization_handshake(session_endpoint, lines_iter, verbose):
            if verbose:
                click.echo("MCP initialization failed", err=True)
            sse_response.close()
            return None
        
        if verbose:
            click.echo("Sending request to session endpoint...", err=True)
        
        # Now POST the actual request to the session endpoint
        post_response = requests.post(
            session_endpoint,
            json=jsonrpc_request,
            headers={'Content-Type': 'application/json'},
            timeout=SSE_POST_TIMEOUT
        )
        
        # 202 Accepted means response will come via SSE stream
        # 200 OK might mean immediate response
        if post_response.status_code not in (200, 202):
            if verbose:
                click.echo(f"Session POST failed: {post_response.status_code}", err=True)
            sse_response.close()
            return None
        
        # Continue reading from the SSE stream for the response
        request_id = jsonrpc_request.get('id')
        response_data = None
        
        # Set a timeout for reading the response
        start_time = time.time()
        
        if verbose:
            click.echo("Reading response from SSE stream...", err=True)
        
        # Continue reading from the same iterator
        for line in lines_iter:
            if time.time() - start_time > SSE_READ_TIMEOUT:
                if verbose:
                    click.echo("Timeout waiting for response in SSE stream", err=True)
                break
            
            if not line:
                continue
            
            if verbose and line:
                # Truncate long lines for readability
                display_line = line[:SSE_LOG_TRUNCATE_LENGTH] if len(line) > SSE_LOG_TRUNCATE_LENGTH else line
                click.echo(f"SSE line: {display_line}", err=True)
            
            if line.startswith('data: '):
                data_str = line[6:]  # Remove 'data: ' prefix
                try:
                    message = json.loads(data_str)
                    # Look for JSON-RPC response matching our request ID
                    if isinstance(message, dict) and message.get('id') == request_id:
                        response_data = message
                        if verbose:
                            click.echo("Found matching response!", err=True)
                        break
                except json.JSONDecodeError:
                    continue
        
        # Close the SSE connection
        sse_response.close()
        
        return response_data
        
    except requests.exceptions.RequestException:
        if verbose:
            click.echo("Session-based SSE connection failed", err=True)
        return None
    except Exception as e:
        if verbose:
            click.echo(f"Session-based SSE error: {e}", err=True)
        return None


@click.command()
@click.argument('url')
@click.option('-d', '--data', 'data_flags', multiple=True, 
              help='Add data to the request. Format: key=value or JSON string')
@click.option('-H', '--header', 'header_flags', multiple=True,
              help='Add custom HTTP header. Format: "Key: Value"')
@click.option('-v', '--verbose', is_flag=True,
              help='Enable verbose output (prints JSON-RPC payload and HTTP headers to stderr)')
@click.version_option(version=__version__)
def main(url: str, data_flags: Tuple[str, ...], header_flags: Tuple[str, ...], verbose: bool):
    """murl - MCP Curl: A curl-like CLI tool for Model Context Protocol (MCP) servers.

    MCP (Model Context Protocol) is an open standard for AI models to access
    external data sources and tools. murl provides a command-line interface
    to interact with MCP servers.
    
    Examples:
    
        # List tools
        murl http://localhost:3000/tools
        
        # Call a tool
        murl http://localhost:3000/tools/echo -d message=hello
        
        # Call a tool with JSON data
        murl http://localhost:3000/tools/config -d '{"theme": "dark"}'
        
        # Read a resource (file path)
        murl http://localhost:3000/resources/path/to/file
        
        # Add authorization header
        murl http://localhost:3000/prompts -H "Authorization: Bearer token123"
    """
    try:
        # Parse URL
        base_url, virtual_path = parse_url(url)
        
        # Parse data flags
        data = parse_data_flags(data_flags) if data_flags else {}
        
        # Map virtual path to method and params
        method, params = map_virtual_path_to_method(virtual_path, data)
        
        # Create JSON-RPC request
        jsonrpc_request = create_jsonrpc_request(method, params)
        
        # Parse headers
        headers = parse_headers(header_flags) if header_flags else {}
        headers['Content-Type'] = 'application/json'
        # Support Streamable HTTP transport (MCP protocol)
        # This allows murl to work with both regular HTTP JSON-RPC servers
        # and MCP servers using Streamable HTTP transport (like mcp-proxy)
        headers['Accept'] = 'application/json, text/event-stream'
        
        # Verbose output
        if verbose:
            click.echo("=== JSON-RPC Request ===", err=True)
            click.echo(json.dumps(jsonrpc_request, indent=2), err=True)
            click.echo("\n=== HTTP Headers ===", err=True)
            click.echo(json.dumps(headers, indent=2), err=True)
            click.echo("\n=== Sending to ===", err=True)
            click.echo(base_url, err=True)
            click.echo("", err=True)
        
        # Try session-based SSE first (for mcp-proxy compatibility)
        response_data = try_session_based_sse_request(
            base_url, jsonrpc_request, headers, verbose
        )
        
        if response_data is not None:
            # Successfully got response via session-based SSE
            if verbose:
                click.echo("=== Response via Session-Based SSE ===", err=True)
                click.echo("", err=True)
        else:
            # Fall back to regular HTTP POST request
            if verbose:
                click.echo("=== Using Regular HTTP POST ===", err=True)
            
            response = requests.post(
                base_url,
                json=jsonrpc_request,
                headers=headers,
                timeout=30
            )
            
            # Verbose response headers
            if verbose:
                click.echo("=== HTTP Response Headers ===", err=True)
                click.echo(json.dumps(dict(response.headers), indent=2), err=True)
                click.echo("", err=True)
            
            # Parse response
            content_type = response.headers.get('Content-Type', '')
            request_id = jsonrpc_request.get('id')
            
            # Handle SSE response (Streamable HTTP transport)
            if 'text/event-stream' in content_type:
                # Parse SSE stream and extract JSON-RPC messages
                response_data = None
                for line in response.text.split('\n'):
                    line = line.strip()
                    if line.startswith('data: '):
                        data_str = line[6:]  # Remove 'data: ' prefix
                        try:
                            message = json.loads(data_str)
                            # Look for JSON-RPC response matching our request ID
                            if isinstance(message, dict) and message.get('id') == request_id:
                                # Found a matching response, use it
                                response_data = message
                                break  # Use first matching response
                        except json.JSONDecodeError:
                            continue
                
                if response_data is None:
                    click.echo(f"Error: No valid JSON-RPC response in SSE stream", err=True)
                    click.echo(f"Response: {response.text}", err=True)
                    sys.exit(1)
            else:
                # Regular JSON response
                try:
                    response_data = response.json()
                except json.JSONDecodeError:
                    click.echo(f"Error: Invalid JSON response from server", err=True)
                    click.echo(f"Status Code: {response.status_code}", err=True)
                    click.echo(f"Response: {response.text}", err=True)
                    sys.exit(1)
        
        # Check for JSON-RPC error
        if 'error' in response_data:
            error = response_data['error']
            click.echo(f"MCP Error: {error.get('message', 'Unknown error')}", err=True)
            if 'code' in error:
                click.echo(f"Error Code: {error['code']}", err=True)
            if 'data' in error:
                click.echo(f"Error Data: {json.dumps(error['data'])}", err=True)
            sys.exit(1)
        
        # Output the result
        if 'result' in response_data:
            # Pretty print JSON to stdout
            click.echo(json.dumps(response_data['result'], indent=2))
        else:
            # No result field, print entire response
            click.echo(json.dumps(response_data, indent=2))
        
    except requests.exceptions.ConnectionError:
        click.echo(f"Error: Connection refused to {url}", err=True)
        sys.exit(1)
    except requests.exceptions.Timeout:
        click.echo(f"Error: Request timeout to {url}", err=True)
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        click.echo(f"Error: HTTP request failed: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
