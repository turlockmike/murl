"""CLI entry point for murl."""

import json
import re
import sys
from typing import Dict, Any, Tuple

import click
import requests
from murl import __version__


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
        elif len(parts) == 2 and parts[1] == 'read':
            # /resources/read -> resources/read
            return 'resources/read', data
        else:
            raise ValueError(f"Invalid resources path: {virtual_path}")
    
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
        
        # Verbose output
        if verbose:
            click.echo("=== JSON-RPC Request ===", err=True)
            click.echo(json.dumps(jsonrpc_request, indent=2), err=True)
            click.echo("\n=== HTTP Headers ===", err=True)
            click.echo(json.dumps(headers, indent=2), err=True)
            click.echo("\n=== Sending to ===", err=True)
            click.echo(base_url, err=True)
            click.echo("", err=True)
        
        # Make HTTP POST request
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
