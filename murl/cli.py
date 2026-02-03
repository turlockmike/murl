"""CLI entry point for murl."""

import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Dict, Any, Tuple, Optional

# Python 3.10 compatibility: ExceptionGroup was added in 3.11
try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup

import click
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from murl import __version__


# Error patterns for connection failures
# Note: These patterns are based on httpx/httpcore error messages and may need
# updates if the underlying library changes its error message formats
DNS_ERROR_PATTERNS = [
    "No address associated with hostname",
    "Name or service not known",
    "nodename nor servname provided",
]

CONNECTION_REFUSED_PATTERNS = [
    "Connection refused",
    "All connection attempts failed",
]


# Error code constants for better categorization
class ErrorCode:
    """Error code constants for agent mode."""
    SUCCESS = 0
    GENERAL_ERROR = 1
    INVALID_ARGUMENT = 2
    MCP_SERVER_ERROR = 100  # Used in JSON 'code' field, not as exit code


def output_error(agent_mode: bool, error_type: str, message: str, exit_code: int, 
                 suggestion: Optional[str] = None, url: Optional[str] = None) -> None:
    """Output an error message in agent or human mode and exit.
    
    Args:
        agent_mode: Whether agent mode is enabled
        error_type: Error type constant (e.g., "INVALID_ARGUMENT", "CONNECTION_ERROR")
        message: Human-readable error message
        exit_code: Exit code to use (0, 1, 2)
        suggestion: Optional suggestion for recovery (agent mode only)
        url: Optional URL for context (human mode only)
    """
    if agent_mode:
        error_obj = {
            "error": error_type,
            "message": message,
            "code": exit_code
        }
        if suggestion:
            error_obj["suggestion"] = suggestion
        click.echo(json.dumps(error_obj), err=True)
    else:
        # Human mode: friendly error messages
        if url:
            click.echo(f"Error: {message} ({url})", err=True)
        else:
            click.echo(f"Error: {message}", err=True)
    sys.exit(exit_code)


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
    headers: Dict[str, str],
    verbose: bool
) -> Any:
    """Make an MCP request using the official SDK.
    
    Args:
        base_url: The base URL of the MCP server
        method: The MCP method to call
        params: The method parameters
        headers: Custom HTTP headers to include in requests
        verbose: Whether to print verbose output
        
    Returns:
        The result from the MCP server (as JSON-serializable data)
        
    Raises:
        Exception: If the request fails
    """
    # Validate required parameters before making connection
    if method == 'tools/call':
        if params.get('name') is None:
            raise ValueError("Missing required 'name' parameter for tools/call method")
    elif method == 'resources/read':
        if params.get('uri') is None:
            raise ValueError("Missing required 'uri' parameter for resources/read method")
    elif method == 'prompts/get':
        if params.get('name') is None:
            raise ValueError("Missing required 'name' parameter for prompts/get request")
    
    if verbose:
        click.echo("=== MCP Request ===", err=True)
        click.echo(f"Method: {method}", err=True)
        click.echo(f"Params: {json.dumps(params, indent=2)}", err=True)
        click.echo(f"URL: {base_url}", err=True)
        if headers:
            click.echo(f"Headers: {json.dumps(headers, indent=2)}", err=True)
        click.echo("", err=True)
    
    # Create httpx client with custom headers if provided
    import httpx
    http_client = None
    if headers:
        http_client = httpx.AsyncClient(headers=headers)
    
    try:
        async with streamable_http_client(base_url, http_client=http_client) as (read, write, get_session_id):
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
    finally:
        # Clean up the http_client if we created one
        if http_client is not None:
            await http_client.aclose()



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
    
    click.echo("Upgrading murl...")
    
    # Helper function for error handling
    def show_error_and_exit(error_msg: str):
        click.echo(f"Error: {error_msg}", err=True)
        click.echo("Please try upgrading manually with: pip install --upgrade mcp-curl", err=True)
        ctx.exit(1)
    
    # Use pip to upgrade mcp-curl package
    # Set a reasonable timeout to prevent hanging
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "mcp-curl"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode == 0:
            click.echo(result.stdout)
            click.echo("✓ Upgrade complete!")
            ctx.exit(0)
        else:
            show_error_and_exit(f"Upgrade failed:\n{result.stderr}")
    except subprocess.TimeoutExpired:
        show_error_and_exit("Upgrade timed out after 5 minutes.")


def set_agent_mode(ctx, param, value):
    """Set agent mode in context for help callback to use."""
    if value:
        ctx.ensure_object(dict)
        ctx.obj['agent_mode'] = True
    return value


def handle_help(ctx, param, value):
    """Handle help display with agent mode awareness."""
    if not value or ctx.resilient_parsing:
        return
    
    # Check if agent mode was set in context
    agent_mode = ctx.obj and ctx.obj.get('agent_mode', False) if ctx.obj else False
    
    if agent_mode:
        show_agent_help()
    else:
        click.echo(ctx.get_help())
    
    ctx.exit()


def show_agent_help():
    """Show agent-optimized help output (POSIX Agent Standard Level 2)."""
    help_text = """USAGE:
  murl [--agent] <url> [OPTIONS]

DESCRIPTION:
  MCP Curl - A curl-like CLI tool for Model Context Protocol (MCP) servers.
  When --agent flag is used, outputs pure JSON to stdout and structured errors to stderr.

COMMON PATTERNS:
  murl --agent http://localhost:3000/tools                           # List tools (JSON Lines)
  murl --agent http://localhost:3000/tools/echo -d message=hello     # Call tool
  murl --agent http://localhost:3000/tools/weather -d city=Boston    # Call with arguments
  murl --agent http://localhost:3000/resources/path/to/file          # Read resource
  murl --agent http://localhost:3000/prompts/greeting -d name=Alice  # Get prompt
  murl --agent http://localhost:3000/tools | jq -c '.[]'             # Process with jq

OPTIONS:
  -d, --data <key=value>     Add data to request (multiple allowed)
  -H, --header <Key: Value>  Add custom HTTP header (multiple allowed)
  -v, --verbose              Enable verbose output (to stderr)
  --agent                    Agent-compatible mode (pure JSON, structured errors)
  --version                  Show version information
  --help                     Show help message

URL PATHS:
  /tools              List all tools
  /tools/<name>       Call a specific tool
  /resources          List all resources
  /resources/<path>   Read a specific resource
  /prompts            List all prompts
  /prompts/<name>     Get a specific prompt

OUTPUT FORMAT:
  Success: Pure JSON to stdout (compact in agent mode, indented in human mode)
  List operations: JSON Lines (NDJSON) - one JSON object per line
  Errors: JSON object to stderr with structure: {"error": "CODE", "message": "...", "code": N}

ERROR CODES:
  0    Success
  1    General error (connection, timeout, validation)
  2    Invalid arguments (malformed URL, invalid data format)
  100  MCP server error (tool not found, resource unavailable)

ANTI-PATTERNS:
  murl http://localhost:3000                        # Missing MCP path (/tools, /resources, /prompts)
  murl --agent http://localhost:3000/tools --data   # Invalid --data without key=value
  murl http://localhost:3000/tools -d [1,2,3]       # JSON arrays not supported in -d flag
"""
    click.echo(help_text)


@click.command()
@click.argument('url', required=False)
@click.option('-d', '--data', 'data_flags', multiple=True, 
              help='Add data to the request. Format: key=value or JSON string')
@click.option('-H', '--header', 'header_flags', multiple=True,
              help='Add custom HTTP header. Format: "Key: Value"')
@click.option('-v', '--verbose', is_flag=True,
              help='Enable verbose output (prints JSON-RPC payload and HTTP headers to stderr)')
@click.option('--agent', is_flag=True, callback=set_agent_mode, is_eager=True,
              help='Enable agent-compatible mode (pure JSON output, structured errors)')
@click.option('--help', '-h', 'show_help', is_flag=True, callback=handle_help, expose_value=False, is_eager=True,
              help='Show help message')
@click.option('--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True,
              help='Show detailed version information')
@click.option('--upgrade', is_flag=True, callback=run_upgrade, expose_value=False, is_eager=True,
              help='Upgrade murl to the latest version')
def main(url: Optional[str], data_flags: Tuple[str, ...], header_flags: Tuple[str, ...], verbose: bool, agent: bool):
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
    # If no URL is provided, show error in agent mode or help in human mode
    if url is None:
        if agent:
            output_error(
                agent_mode=True,
                error_type="MISSING_ARGUMENT",
                message="URL argument is required",
                exit_code=ErrorCode.INVALID_ARGUMENT,
                suggestion="Use: murl --agent --help for usage information"
            )
        else:
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
        
        # Parse headers for authentication and custom HTTP headers
        headers = parse_headers(header_flags) if header_flags else {}
        
        # Make the MCP request using SDK (async)
        result = asyncio.run(make_mcp_request(base_url, method, params, headers, verbose))
        
        # Output the result based on mode
        if agent:
            # Agent mode: compact JSON or JSON Lines for lists
            if isinstance(result, list):
                # JSON Lines (NDJSON) format for lists
                for item in result:
                    click.echo(json.dumps(item, separators=(',', ':')))
            else:
                # Single object: compact JSON
                click.echo(json.dumps(result, separators=(',', ':')))
        else:
            # Human mode: pretty-printed JSON with indentation
            click.echo(json.dumps(result, indent=2))
        
    except ValueError as e:
        output_error(
            agent_mode=agent,
            error_type="INVALID_ARGUMENT",
            message=str(e),
            exit_code=ErrorCode.INVALID_ARGUMENT
        )
    except ConnectionError as e:
        output_error(
            agent_mode=agent,
            error_type="CONNECTION_ERROR",
            message=f"Failed to connect: {str(e)}" if agent else f"Failed to connect to {url}: {e}",
            exit_code=ErrorCode.GENERAL_ERROR,
            url=url if not agent else None
        )
    except TimeoutError as e:
        output_error(
            agent_mode=agent,
            error_type="TIMEOUT",
            message=f"Request timeout: {str(e)}" if agent else f"Request timeout to {url}: {e}",
            exit_code=ErrorCode.GENERAL_ERROR,
            url=url if not agent else None
        )
    except ExceptionGroup as eg:
        # Handle ExceptionGroup from async tasks (MCP SDK uses anyio TaskGroups)
        # Extract the first underlying exception for better error messages
        if eg.exceptions:
            exc = eg.exceptions[0]
            exc_type = type(exc).__name__
            exc_msg = str(exc)
            
            # Extract hostname from base_url for better error messages
            parsed_url = urllib.parse.urlparse(base_url)
            hostname = parsed_url.hostname
            if not hostname:
                netloc = parsed_url.netloc
                if netloc:
                    # Strip optional user info and port from netloc
                    host_port = netloc.rsplit("@", 1)[-1]
                    hostname = host_port.split(":", 1)[0] or "unknown host"
                else:
                    hostname = "unknown host"
            
            # Determine error type and message based on exception
            if exc_type == "ConnectError":
                if any(pattern in exc_msg for pattern in DNS_ERROR_PATTERNS):
                    error_type = "DNS_RESOLUTION_FAILED"
                    agent_msg = f"DNS resolution failed for host: {hostname}"
                    human_msg = f"Could not connect to server at {base_url}\n       DNS resolution failed for host: {hostname}"
                elif any(pattern in exc_msg for pattern in CONNECTION_REFUSED_PATTERNS):
                    error_type = "CONNECTION_REFUSED"
                    agent_msg = f"Connection refused by host: {hostname}"
                    human_msg = f"Could not connect to server at {base_url}\n       Connection refused by host: {hostname}"
                else:
                    error_type = "CONNECTION_ERROR"
                    agent_msg = str(exc_msg)
                    human_msg = f"Could not connect to server at {base_url}\n       {exc_msg}"
            elif (exc_type == "TimeoutError") or ("Timeout" in exc_msg):
                error_type = "TIMEOUT"
                agent_msg = "Request timeout"
                human_msg = f"Request timeout to {base_url}"
            else:
                error_type = exc_type.upper()
                agent_msg = str(exc_msg)
                human_msg = str(exc_msg)
            
            # Output using helper function
            if agent:
                error_obj = {
                    "error": error_type,
                    "message": agent_msg,
                    "code": ErrorCode.GENERAL_ERROR
                }
                click.echo(json.dumps(error_obj), err=True)
            else:
                # For human mode with multi-line messages, output directly
                click.echo(f"Error: {human_msg}", err=True)
            sys.exit(ErrorCode.GENERAL_ERROR)
        else:
            output_error(
                agent_mode=agent,
                error_type="EXCEPTION_GROUP",
                message=str(eg),
                exit_code=ErrorCode.GENERAL_ERROR
            )
    except Exception as e:
        # Handle MCP SDK exceptions and other errors
        error_msg = str(e)
        
        # Determine error type and exit code
        if "ValidationError" in error_msg:
            error_type = "VALIDATION_ERROR"
            agent_msg = f"Invalid response from server: {error_msg}"
            human_msg = f"Invalid response from server: {e}"
            # Use code 100 in JSON but exit with 1
            if agent:
                error_obj = {
                    "error": error_type,
                    "message": agent_msg,
                    "code": ErrorCode.MCP_SERVER_ERROR
                }
                click.echo(json.dumps(error_obj), err=True)
                sys.exit(ErrorCode.GENERAL_ERROR)
            else:
                click.echo(f"Error: {human_msg}", err=True)
                sys.exit(ErrorCode.GENERAL_ERROR)
        elif "ConnectError" in error_msg or "Connection" in error_msg:
            output_error(
                agent_mode=agent,
                error_type="CONNECTION_ERROR",
                message="Failed to connect" if agent else f"Failed to connect to {url}",
                exit_code=ErrorCode.GENERAL_ERROR,
                url=url if not agent else None
            )
        elif "Timeout" in error_msg:
            output_error(
                agent_mode=agent,
                error_type="TIMEOUT",
                message="Request timeout" if agent else f"Request timeout to {url}",
                exit_code=ErrorCode.GENERAL_ERROR,
                url=url if not agent else None
            )
        else:
            output_error(
                agent_mode=agent,
                error_type="ERROR",
                message=error_msg,
                exit_code=ErrorCode.GENERAL_ERROR
            )


if __name__ == "__main__":
    main()
