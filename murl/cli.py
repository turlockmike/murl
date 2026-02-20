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
from murl.token_store import get_credentials, save_credentials, clear_credentials, is_expired
from murl.auth import authorize, refresh_token, OAuthError


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


# Error code constants
class ErrorCode:
    SUCCESS = 0
    GENERAL_ERROR = 1
    INVALID_ARGUMENT = 2
    MCP_SERVER_ERROR = 100  # Used in JSON 'code' field, not as exit code


def output_error(error_type: str, message: str, exit_code: int,
                 suggestion: Optional[str] = None) -> None:
    """Output a structured JSON error to stderr and exit."""
    error_obj = {
        "error": error_type,
        "message": message,
        "code": exit_code
    }
    if suggestion:
        error_obj["suggestion"] = suggestion
    click.echo(json.dumps(error_obj), err=True)
    sys.exit(exit_code)


def parse_url(full_url: str) -> Tuple[str, str]:
    """Parse the full URL into base URL and virtual path.

    Returns:
        Tuple of (base_url, virtual_path)

    Raises:
        ValueError: If the URL doesn't contain a valid MCP path
    """
    pattern = r'/(tools|resources|prompts)(\/.*)?$'
    match = re.search(pattern, full_url)

    if not match:
        raise ValueError(
            "Invalid MCP URL. Must contain /tools, /resources, or /prompts"
        )

    virtual_path = match.group(0)
    base_url = full_url[:match.start()]

    return base_url, virtual_path


def parse_data_value(value: str) -> Any:
    """Parse a data value and coerce types."""
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False

    try:
        if '.' not in value:
            return int(value)
        return float(value)
    except ValueError:
        pass

    return value


def parse_data_flags(data_flags: Tuple[str, ...]) -> Dict[str, Any]:
    """Parse -d/--data flags into a dictionary.

    Note:
        JSON objects (starting with '{') are merged into the result.
        JSON arrays (starting with '[') are not supported as they don't
        represent key-value pairs needed for MCP arguments.
    """
    result = {}

    for data in data_flags:
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
            if '=' not in data:
                raise ValueError(f"Invalid data format: {data}. Expected key=value or JSON")

            key, value = data.split('=', 1)
            result[key] = parse_data_value(value)

    return result


def map_virtual_path_to_method(virtual_path: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Map a virtual path to an MCP JSON-RPC method and params."""
    parts = virtual_path.lstrip('/').split('/')

    if not parts or parts == ['']:
        raise ValueError("Invalid virtual path: empty path")

    category = parts[0]

    if category == 'tools':
        if len(parts) == 1:
            return 'tools/list', {}
        else:
            tool_name = parts[1]
            return 'tools/call', {
                'name': tool_name,
                'arguments': data
            }

    elif category == 'resources':
        if len(parts) == 1:
            return 'resources/list', {}
        else:
            file_path = '/'.join(parts[1:])
            if not file_path or file_path == '':
                raise ValueError("Invalid resources path: path cannot be empty after /resources/")
            if not file_path.startswith('/'):
                file_path = '/' + file_path
            uri = f'file://{file_path}'
            return 'resources/read', {'uri': uri, **data}

    elif category == 'prompts':
        if len(parts) == 1:
            return 'prompts/list', {}
        else:
            prompt_name = parts[1]
            return 'prompts/get', {
                'name': prompt_name,
                'arguments': data
            }

    else:
        raise ValueError(f"Invalid MCP category: {category}")


def parse_headers(header_flags: Tuple[str, ...]) -> Dict[str, str]:
    """Parse -H/--header flags into a dictionary."""
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
    """Make an MCP request using the official SDK."""
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
                init_result = await session.initialize()

                if verbose:
                    click.echo("=== MCP Initialization ===", err=True)
                    click.echo(f"Protocol Version: {init_result.protocolVersion}", err=True)
                    click.echo(f"Server: {init_result.serverInfo.name} {init_result.serverInfo.version}", err=True)
                    click.echo("", err=True)

                if method == 'tools/list':
                    result = await session.list_tools()
                    return [tool.model_dump(mode='json', exclude_none=True) for tool in result.tools]
                elif method == 'tools/call':
                    tool_name = params.get('name')
                    arguments = params.get('arguments', {})
                    result = await session.call_tool(tool_name, arguments)
                    return [content.model_dump(mode='json', exclude_none=True) for content in result.content]
                elif method == 'resources/list':
                    result = await session.list_resources()
                    return [resource.model_dump(mode='json', exclude_none=True) for resource in result.resources]
                elif method == 'resources/read':
                    uri = params.get('uri')
                    result = await session.read_resource(uri)
                    return [content.model_dump(mode='json', exclude_none=True) for content in result.contents]
                elif method == 'prompts/list':
                    result = await session.list_prompts()
                    return [prompt.model_dump(mode='json', exclude_none=True) for prompt in result.prompts]
                elif method == 'prompts/get':
                    prompt_name = params.get('name')
                    arguments = params.get('arguments', {})
                    result = await session.get_prompt(prompt_name, arguments)
                    return [message.model_dump(mode='json', exclude_none=True) for message in result.messages]
                else:
                    raise ValueError(f"Unsupported method: {method}")
    finally:
        if http_client is not None:
            await http_client.aclose()


def print_version(ctx, param, value):
    """Print detailed version information."""
    if not value or ctx.resilient_parsing:
        return

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

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

    def show_error_and_exit(error_msg: str):
        click.echo(f"Error: {error_msg}", err=True)
        click.echo("Please try upgrading manually with: pip install --upgrade mcp-curl", err=True)
        ctx.exit(1)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "mcp-curl"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300
        )

        if result.returncode == 0:
            click.echo(result.stdout)
            click.echo("Upgrade complete!")
            ctx.exit(0)
        else:
            show_error_and_exit(f"Upgrade failed:\n{result.stderr}")
    except subprocess.TimeoutExpired:
        show_error_and_exit("Upgrade timed out after 5 minutes.")


def show_help(ctx, param, value):
    """Show help output."""
    if not value or ctx.resilient_parsing:
        return

    help_text = """USAGE:
  murl <url> [OPTIONS]

DESCRIPTION:
  MCP Curl - CLI for querying Model Context Protocol (MCP) servers.
  Outputs compact JSON to stdout, structured errors to stderr.

EXAMPLES:
  murl https://server.com/mcp/tools                         # List tools
  murl https://server.com/mcp/tools/echo -d message=hello   # Call tool
  murl https://server.com/mcp/resources/path/to/file         # Read resource
  murl https://server.com/mcp/prompts/greeting -d name=Alice # Get prompt

AUTHENTICATION:
  OAuth 2.0 (RFC 7591) with PKCE is built in.
  Credentials auto-refresh. On 401, re-authenticates and retries once.

  murl --login https://server.com/mcp/tools    # First-time auth (opens browser)
  murl https://server.com/mcp/tools            # Uses stored token
  murl --no-auth https://server.com/mcp/tools  # Skip auth
  murl -H "Authorization: Bearer <tok>" <url>  # Manual token

  Credentials: ~/.murl/credentials/<hash>.json

OPTIONS:
  -d, --data <key=value|JSON>  Request data (repeatable)
  -H, --header <Key: Value>    HTTP header (repeatable)
  -v, --verbose                Pretty-print output, show request debug info
  --login                      Force OAuth re-authentication
  --no-auth                    Skip all authentication
  --version                    Version info
  --upgrade                    Self-upgrade via pip
  -h, --help                   This help

URL PATHS:
  /tools            List tools         /tools/<name>       Call tool
  /resources        List resources     /resources/<path>   Read resource
  /prompts          List prompts       /prompts/<name>     Get prompt

OUTPUT:
  stdout  Compact JSON (NDJSON for lists). Pretty-printed with -v.
  stderr  Errors as {"error":"CODE","message":"...","code":N}
  exit    0=success  1=error  2=invalid args"""
    click.echo(help_text)
    ctx.exit()


@click.command()
@click.argument('url', required=False)
@click.option('-d', '--data', 'data_flags', multiple=True,
              help='Request data. Format: key=value or JSON object')
@click.option('-H', '--header', 'header_flags', multiple=True,
              help='HTTP header. Format: "Key: Value"')
@click.option('-v', '--verbose', is_flag=True,
              help='Pretty-print output and show request debug info')
@click.option('--help', '-h', 'help_', is_flag=True, callback=show_help, expose_value=False, is_eager=True,
              help='Show help')
@click.option('--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True,
              help='Show version')
@click.option('--upgrade', is_flag=True, callback=run_upgrade, expose_value=False, is_eager=True,
              help='Upgrade murl')
@click.option('--login', is_flag=True, help='Force OAuth re-authentication')
@click.option('--no-auth', is_flag=True, help='Skip all authentication')
def main(url: Optional[str], data_flags: Tuple[str, ...], header_flags: Tuple[str, ...],
         verbose: bool, login: bool, no_auth: bool):
    """murl - MCP Curl"""
    if url is None:
        output_error(
            error_type="MISSING_ARGUMENT",
            message="URL argument is required",
            exit_code=ErrorCode.INVALID_ARGUMENT,
            suggestion="Run: murl --help"
        )

    try:
        base_url, virtual_path = parse_url(url)
        data = parse_data_flags(data_flags) if data_flags else {}
        method, params = map_virtual_path_to_method(virtual_path, data)
        headers = parse_headers(header_flags) if header_flags else {}

        # --- Auth ---
        if not no_auth and 'Authorization' not in headers:
            if login:
                clear_credentials(base_url)

            creds = get_credentials(base_url)

            if creds and not login:
                if is_expired(creds):
                    try:
                        creds = refresh_token(creds)
                        save_credentials(base_url, creds)
                    except OAuthError:
                        creds = authorize(base_url)
                        save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
            elif login:
                creds = authorize(base_url)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"

        # --- Request with 401 retry ---
        try:
            result = asyncio.run(make_mcp_request(base_url, method, params, headers, verbose))
        except (Exception, ExceptionGroup) as req_err:
            err_str = str(req_err)
            if not no_auth and ("401" in err_str or "Unauthorized" in err_str):
                if verbose:
                    click.echo("Received 401 — initiating OAuth flow...", err=True)
                creds = authorize(base_url)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
                result = asyncio.run(make_mcp_request(base_url, method, params, headers, verbose))
            else:
                raise

        # --- Output ---
        if verbose:
            click.echo(json.dumps(result, indent=2))
        elif isinstance(result, list):
            for item in result:
                click.echo(json.dumps(item, separators=(',', ':')))
        else:
            click.echo(json.dumps(result, separators=(',', ':')))

    except ValueError as e:
        output_error(
            error_type="INVALID_ARGUMENT",
            message=str(e),
            exit_code=ErrorCode.INVALID_ARGUMENT
        )
    except ConnectionError as e:
        output_error(
            error_type="CONNECTION_ERROR",
            message=f"Failed to connect: {e}",
            exit_code=ErrorCode.GENERAL_ERROR
        )
    except TimeoutError as e:
        output_error(
            error_type="TIMEOUT",
            message=f"Request timeout: {e}",
            exit_code=ErrorCode.GENERAL_ERROR
        )
    except ExceptionGroup as eg:
        if eg.exceptions:
            exc = eg.exceptions[0]
            exc_type = type(exc).__name__
            exc_msg = str(exc)

            parsed_url = urllib.parse.urlparse(base_url)
            hostname = parsed_url.hostname
            if not hostname:
                netloc = parsed_url.netloc
                if netloc:
                    host_port = netloc.rsplit("@", 1)[-1]
                    hostname = host_port.split(":", 1)[0] or "unknown host"
                else:
                    hostname = "unknown host"

            if exc_type == "ConnectError":
                if any(p in exc_msg for p in DNS_ERROR_PATTERNS):
                    error_type = "DNS_RESOLUTION_FAILED"
                    msg = f"DNS resolution failed for host: {hostname}"
                elif any(p in exc_msg for p in CONNECTION_REFUSED_PATTERNS):
                    error_type = "CONNECTION_REFUSED"
                    msg = f"Connection refused by host: {hostname}"
                else:
                    error_type = "CONNECTION_ERROR"
                    msg = exc_msg
            elif exc_type == "TimeoutError" or "Timeout" in exc_msg:
                error_type = "TIMEOUT"
                msg = "Request timeout"
            else:
                error_type = exc_type.upper()
                msg = exc_msg

            error_obj = {
                "error": error_type,
                "message": msg,
                "code": ErrorCode.GENERAL_ERROR
            }
            click.echo(json.dumps(error_obj), err=True)
            sys.exit(ErrorCode.GENERAL_ERROR)
        else:
            output_error(
                error_type="EXCEPTION_GROUP",
                message=str(eg),
                exit_code=ErrorCode.GENERAL_ERROR
            )
    except Exception as e:
        error_msg = str(e)

        if "ValidationError" in error_msg:
            error_obj = {
                "error": "VALIDATION_ERROR",
                "message": f"Invalid response from server: {error_msg}",
                "code": ErrorCode.MCP_SERVER_ERROR
            }
            click.echo(json.dumps(error_obj), err=True)
            sys.exit(ErrorCode.GENERAL_ERROR)
        elif "ConnectError" in error_msg or "Connection" in error_msg:
            output_error(
                error_type="CONNECTION_ERROR",
                message="Failed to connect",
                exit_code=ErrorCode.GENERAL_ERROR
            )
        elif "Timeout" in error_msg:
            output_error(
                error_type="TIMEOUT",
                message="Request timeout",
                exit_code=ErrorCode.GENERAL_ERROR
            )
        else:
            output_error(
                error_type="ERROR",
                message=error_msg,
                exit_code=ErrorCode.GENERAL_ERROR
            )


if __name__ == "__main__":
    main()
