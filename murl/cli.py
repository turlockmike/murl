"""CLI entry point for murl."""

import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Dict, Any, Tuple, Optional

import click
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
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


async def make_mcp_request(
    base_url: str,
    method: str,
    params: Dict[str, Any],
    verbose: bool
) -> Any:
    """Make an MCP request using the official SDK.
    
    Args:
        base_url: The base URL of the MCP server
        method: The MCP method to call
        params: The method parameters
        verbose: Whether to print verbose output
        
    Returns:
        The result from the MCP server (as JSON-serializable data)
        
    Raises:
        Exception: If the request fails
    """
    if verbose:
        click.echo("=== MCP Request ===", err=True)
        click.echo(f"Method: {method}", err=True)
        click.echo(f"Params: {json.dumps(params, indent=2)}", err=True)
        click.echo(f"URL: {base_url}", err=True)
        click.echo("", err=True)
    
    async with streamable_http_client(base_url) as (read, write, get_session_id):
        async with ClientSession(read, write) as session:
            # Initialize the session
            init_result = await session.initialize()
            
            if verbose:
                click.echo("=== MCP Initialization ===", err=True)
                click.echo(f"Protocol Version: {init_result.protocolVersion}", err=True)
                click.echo(f"Server: {init_result.serverInfo.name} {init_result.serverInfo.version}", err=True)
                click.echo("", err=True)
            
            # Route to appropriate SDK method
            if method == 'tools/list':
                result = await session.list_tools()
                # Convert pydantic models to dict
                return [tool.model_dump(mode='json', exclude_none=True) for tool in result.tools]
            elif method == 'tools/call':
                tool_name = params.get('name')
                arguments = params.get('arguments', {})
                result = await session.call_tool(tool_name, arguments)
                # Convert content to dict
                return [content.model_dump(mode='json', exclude_none=True) for content in result.content]
            elif method == 'resources/list':
                result = await session.list_resources()
                # Convert resources to dict
                return [resource.model_dump(mode='json', exclude_none=True) for resource in result.resources]
            elif method == 'resources/read':
                uri = params.get('uri')
                result = await session.read_resource(uri)
                # Convert contents to dict
                return [content.model_dump(mode='json', exclude_none=True) for content in result.contents]
            elif method == 'prompts/list':
                result = await session.list_prompts()
                # Convert prompts to dict
                return [prompt.model_dump(mode='json', exclude_none=True) for prompt in result.prompts]
            elif method == 'prompts/get':
                prompt_name = params.get('name')
                arguments = params.get('arguments', {})
                result = await session.get_prompt(prompt_name, arguments)
                # Convert messages to dict
                return [message.model_dump(mode='json', exclude_none=True) for message in result.messages]
            else:
                raise ValueError(f"Unsupported method: {method}")



def print_version(ctx, param, value):
    """Print detailed version information."""
    if not value or ctx.resilient_parsing:
        return
    
    # Get Python version
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    
    # Try to get installation path
    try:
        import murl
        install_path = os.path.dirname(os.path.abspath(murl.__file__))
    except Exception:
        install_path = "unknown"
    
    click.echo(f"murl version {__version__}")
    click.echo(f"Python {python_version}")
    click.echo(f"Installation path: {install_path}")
    ctx.exit()


def run_upgrade(ctx, param, value):
    """Run the upgrade process."""
    if not value or ctx.resilient_parsing:
        return
    
    # Repository configuration - using master branch as primary
    github_repo_url = "https://raw.githubusercontent.com/turlockmike/murl/master/install.sh"
    
    try:
        # Check if required tools are available
        try:
            subprocess.run(["curl", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            click.echo("Error: curl is not installed. Please install curl and try again.", err=True)
            ctx.exit(1)
        
        try:
            subprocess.run(["bash", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            click.echo("Error: bash is not installed. Please install bash and try again.", err=True)
            ctx.exit(1)
        
        click.echo("Upgrading murl...")
        click.echo("Downloading and running install script...")
        
        # Use subprocess with list arguments to avoid shell injection
        # First download the script
        download_result = subprocess.run(
            ["curl", "-sSL", github_repo_url],
            capture_output=True,
            text=True,
            check=False
        )
        
        if download_result.returncode != 0:
            click.echo(f"Error: Failed to download install script: {download_result.stderr}", err=True)
            ctx.exit(1)
        
        # Then execute it with bash
        result = subprocess.run(
            ["bash"],
            input=download_result.stdout,
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode == 0:
            click.echo(result.stdout)
            click.echo("✓ Upgrade complete!")
            ctx.exit(0)
        else:
            click.echo(f"Error during upgrade: {result.stderr}", err=True)
            ctx.exit(1)
            
    except subprocess.SubprocessError as e:
        click.echo(f"Error: Subprocess failed: {e}", err=True)
        ctx.exit(1)
    except PermissionError as e:
        click.echo(f"Error: Permission denied: {e}. You may need elevated privileges.", err=True)
        ctx.exit(1)
    except Exception as e:
        click.echo(f"Error: Unexpected error during upgrade: {e}", err=True)
        ctx.exit(1)


@click.command()
@click.argument('url', required=False)
@click.option('-d', '--data', 'data_flags', multiple=True, 
              help='Add data to the request. Format: key=value or JSON string')
@click.option('-H', '--header', 'header_flags', multiple=True,
              help='Add custom HTTP header. Format: "Key: Value"')
@click.option('-v', '--verbose', is_flag=True,
              help='Enable verbose output (prints JSON-RPC payload and HTTP headers to stderr)')
@click.option('--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True,
              help='Show detailed version information')
@click.option('--upgrade', is_flag=True, callback=run_upgrade, expose_value=False, is_eager=True,
              help='Upgrade murl to the latest version')
def main(url: Optional[str], data_flags: Tuple[str, ...], header_flags: Tuple[str, ...], verbose: bool):
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
    # If no URL is provided, show help
    if url is None:
        ctx = click.get_current_context()
        click.echo(ctx.get_help())
        ctx.exit(0)
    
    try:
        # Parse URL
        base_url, virtual_path = parse_url(url)
        
        # Parse data flags
        data = parse_data_flags(data_flags) if data_flags else {}
        
        # Map virtual path to method and params
        method, params = map_virtual_path_to_method(virtual_path, data)
        
        # Parse headers (note: currently unused by MCP SDK but parsed for future compatibility)
        headers = parse_headers(header_flags) if header_flags else {}
        
        # Make the MCP request using SDK (async)
        result = asyncio.run(make_mcp_request(base_url, method, params, verbose))
        
        # Output the result
        click.echo(json.dumps(result, indent=2))
        
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except ConnectionError as e:
        click.echo(f"Error: Failed to connect to {url}: {e}", err=True)
        sys.exit(1)
    except TimeoutError as e:
        click.echo(f"Error: Request timeout to {url}: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        # Handle MCP SDK exceptions and other errors
        error_msg = str(e)
        if "ConnectError" in error_msg or "Connection" in error_msg:
            click.echo(f"Error: Failed to connect to {url}", err=True)
        elif "Timeout" in error_msg:
            click.echo(f"Error: Request timeout to {url}", err=True)
        elif "ValidationError" in error_msg:
            click.echo(f"Error: Invalid response from server: {e}", err=True)
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
