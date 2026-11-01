"""CLI entry point for murl."""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Dict, Any, List, NamedTuple, Tuple, Optional

# The MCP SDK logs a noisy warning when the server returns 404 on session
# DELETE (valid per MCP 2025-11-25 §Session Management — 404 means "session
# already gone").  Suppress it so users don't see spurious errors.
logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)

# Python 3.10 compatibility: ExceptionGroup was added in 3.11
try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup

import click
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    URL_ELICITATION_REQUIRED, DiscoverResult, InputRequiredResult, PaginatedRequestParams,
)
from murl import __version__
import httpx as _httpx
from murl import session_store
from murl.token_store import get_credentials, save_credentials, clear_credentials, is_expired
from murl.auth import authorize, refresh_token, OAuthError, parse_www_authenticate

# Optional TOON format support (pip install "mcp-curl[toon]")
try:
    from toon import encode as toon_encode
except ImportError:
    toon_encode = None


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
                 suggestion: Optional[str] = None,
                 extra: Optional[Dict[str, Any]] = None) -> None:
    """Output a structured JSON error to stderr and exit."""
    error_obj = {
        "error": error_type,
        "message": message,
        "code": exit_code
    }
    if suggestion:
        error_obj["suggestion"] = suggestion
    if extra:
        error_obj.update(extra)
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


_MAX_FILE_READ_BYTES = 10 * 1024 * 1024  # 10 MB


def _read_json_source(source: str) -> dict:
    """Read a JSON object from stdin (@-) or a file (@path).

    Follows the curl convention: -d @- reads stdin, -d @file reads a file.
    The content must be a JSON object (dict), not an array or scalar.
    """
    path = source[1:]  # strip leading @
    try:
        if path == '-':
            content = sys.stdin.read(_MAX_FILE_READ_BYTES + 1)
        else:
            with open(path) as f:
                content = f.read(_MAX_FILE_READ_BYTES + 1)
    except OSError as e:
        raise ValueError(f"Cannot read {source}: {e}") from e

    if len(content) > _MAX_FILE_READ_BYTES:
        raise ValueError(
            f"Input from {source} exceeds 10 MB limit"
        )

    if not content.strip():
        raise ValueError(f"Empty input from {source}")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from {source}: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(
            f"JSON from {source} must be an object, not {type(parsed).__name__}"
        )
    return parsed


def parse_data_flags(data_flags: Tuple[str, ...]) -> Dict[str, Any]:
    """Parse -d/--data flags into a dictionary.

    Supports these formats (processed in order, later values override earlier):
        - key=value       — string value with light type coercion (int/float/bool)
        - key:=value      — raw JSON value (number, bool, null, object, array, string)
        - {"key": "val"}  — inline JSON object, merged into result
        - @-              — read JSON object from stdin (curl convention)
        - @path           — read JSON object from a file

    The ``:=`` (raw-JSON) form is checked before ``=`` so that the key is split
    on the full ``:=`` token. ``-d limit:=5`` yields ``{"limit": 5}`` and
    ``-d filter:='{"a":1}'`` yields ``{"filter": {"a": 1}}``.
    """
    # Detect multiple @- (stdin) references before reading anything.
    # Stdin is a one-shot resource; the second read would always be empty.
    stdin_count = sum(1 for d in data_flags if d.strip() == '@-')
    if stdin_count > 1:
        raise ValueError(
            "stdin (@-) can only be used once (it is consumed on first read)"
        )

    result = {}

    for data in data_flags:
        stripped = data.strip()
        # Check for key:=value (raw JSON) BEFORE key=value, so the key is split
        # on the full ":=" token. Splitting on "=" first would leave a trailing
        # colon on the key (e.g. "limit:" instead of "limit").
        if ':=' in stripped and not stripped.startswith('{') and not stripped.startswith('['):
            key, value = data.split(':=', 1)
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(
                    f"Invalid JSON in raw value for '{key}:=': {value!r}. "
                    f"Use key=value for a plain string."
                )
        # Then key=value, so that e.g. "email=@user" is not mistaken for a
        # @-source read.
        elif '=' in stripped and not stripped.startswith('{') and not stripped.startswith('['):
            key, value = data.split('=', 1)
            result[key] = parse_data_value(value)
        elif stripped.startswith('@'):
            result.update(_read_json_source(stripped))
        elif stripped.startswith('{'):
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
            raise ValueError(f"Invalid data format: {data}. Expected key=value or JSON")

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
            # Join all segments so tool names containing '/' (e.g.
            # "namespace/subtool") aren't truncated to the first segment.
            tool_name = '/'.join(parts[1:])
            return 'tools/call', {
                'name': tool_name,
                'arguments': data
            }

    elif category == 'resources':
        if len(parts) == 1:
            return 'resources/list', {}
        else:
            resource_path = '/'.join(parts[1:])
            if not resource_path or resource_path == '':
                raise ValueError("Invalid resources path: path cannot be empty after /resources/")
            # If the path looks like a URI scheme (contains ://), use it as-is.
            # Otherwise, construct a file:// URI for backwards compatibility.
            if '://' in resource_path:
                uri = resource_path
            else:
                if not resource_path.startswith('/'):
                    resource_path = '/' + resource_path
                uri = f'file://{resource_path}'
            # Allow -d uri=... to override the path-derived URI. Work on a copy
            # so the caller's data dict is never mutated.
            data = dict(data)
            if 'uri' in data:
                uri = data.pop('uri')
            return 'resources/read', {'uri': uri, **data}

    elif category == 'prompts':
        if len(parts) == 1:
            return 'prompts/list', {}
        else:
            # Join all segments so prompt names containing '/' aren't truncated
            # to the first segment.
            prompt_name = '/'.join(parts[1:])
            return 'prompts/get', {
                'name': prompt_name,
                'arguments': data
            }

    else:
        raise ValueError(f"Invalid MCP category: {category}")


# --- v1 method-faithful surface (issue #14) ---
#
# The v1 surface makes the second positional token the LITERAL JSON-RPC method
# string that goes on the wire (`murl <url> tools/call -d name=echo`). The
# method registry below is the source of truth for what a client may issue.
#
# Method strings verified against the MCP spec schemas for 2025-06-18,
# 2025-11-25, and the 2026-07-28 draft. The "core" client→server methods are
# byte-identical across all three, so this surface is a stable contract.

# Client→server methods murl can issue, mapped to how -d data becomes params.
#   'data_as_arguments' — params = {**fixed, 'arguments': data}  (data nests)
#   'data_as_params'    — params = {**fixed, **data}             (data is the params)
#   'no_params'         — params = {} (any -d is an error for this method)
_CLIENT_METHODS: Dict[str, Dict[str, Any]] = {
    'tools/list':              {'shape': 'no_params'},
    'tools/call':              {'shape': 'data_as_arguments', 'requires': ('name',)},
    'resources/list':          {'shape': 'no_params'},
    'resources/templates/list':{'shape': 'no_params'},
    'resources/read':          {'shape': 'data_as_params', 'requires': ('uri',)},
    'prompts/list':            {'shape': 'no_params'},
    'prompts/get':             {'shape': 'data_as_arguments', 'requires': ('name',)},
    # NOTE: only methods make_mcp_request actually dispatches belong here.
    # completion/complete and logging/setLevel are valid MCP client methods but
    # have no handler yet — reachable via --allow-unknown-method until then.
}

# Server→client methods: a client receives these, it cannot initiate them.
# Listed so we can give a precise error instead of a generic "unknown method".
_SERVER_TO_CLIENT_METHODS = frozenset({
    'sampling/createMessage',
    'roots/list',
    'elicitation/create',
})


def build_params_for_method(method: str, name_or_uri: Optional[str],
                            data: Dict[str, Any]) -> Dict[str, Any]:
    """Build JSON-RPC params for a known method from an optional positional
    name/uri token plus -d data. Operates on a copy of `data` (no mutation of
    the caller's dict)."""
    spec = _CLIENT_METHODS[method]
    shape = spec['shape']
    data = dict(data)  # copy — never mutate the caller's dict

    # A positional token after the method is sugar for the method's primary
    # identifier: tools/call & prompts/get -> name, resources/read -> uri.
    if name_or_uri is not None:
        if method in ('tools/call', 'prompts/get'):
            data.setdefault('name', name_or_uri)
        elif method == 'resources/read':
            data.setdefault('uri', name_or_uri)
        else:
            raise ValueError(
                f"'{method}' does not take a positional argument; "
                f"pass data with -d key=value."
            )

    if shape == 'no_params':
        if data:
            raise ValueError(f"'{method}' takes no parameters (got {sorted(data)}).")
        return {}

    for req in spec.get('requires', ()):
        if req not in data:
            raise ValueError(f"'{method}' requires '{req}' (pass -d {req}=... or as the positional argument).")

    if shape == 'data_as_arguments':
        fixed = {k: data.pop(k) for k in spec.get('requires', ()) if k in data}
        return {**fixed, 'arguments': data}
    # data_as_params
    return data


def resolve_invocation(
    args: Tuple[str, ...], data: Dict[str, Any], allow_unknown_method: bool = False
) -> Tuple[str, str, Dict[str, Any], bool]:
    """Resolve CLI positional args into (base_url, method, params, used_legacy).

    Two surfaces, routed unambiguously by whether the second token contains '/'
    (a method always does: namespace/verb):

    1. v1 method-faithful (canonical):
           murl <url> tools/call -d name=echo
           murl <url> tools/list
           murl <url> resources/read -d uri=...     (or:  resources/read <uri>)
       Second token IS the literal JSON-RPC method. Validated against the
       client-issuable method registry; server→client methods are rejected;
       unknown methods need allow_unknown_method=True.

    2. URL-as-method (0.5.x, legacy — soft-deprecated):
           murl <url-ending-in-/tools[/name]>
       A single positional whose URL path ends in /tools|/resources|/prompts.

    Returns used_legacy=True when the legacy form was taken, so the caller can
    emit a deprecation notice.
    """
    if not args:
        raise ValueError("URL argument is required")

    # v1 form: <url> <namespace/method> [<name-or-uri>]. A method token always
    # contains '/'; a legacy single-URL invocation never has a bare second token.
    if len(args) >= 2:
        base_url = args[0]
        method = args[1]
        rest = args[2:]
        if len(rest) > 1:
            raise ValueError(
                f"Too many arguments for '{method}': expected at most one "
                f"name/uri, got {len(rest)} ({' '.join(rest)})"
            )
        name_or_uri = rest[0] if rest else None

        if '/' not in method:
            raise ValueError(
                f"Unrecognized argument '{method}'. The second argument must be "
                f"an MCP method (e.g. 'tools/call', 'resources/read'), or pass a "
                f"single URL ending in /tools, /resources, or /prompts."
            )
        if method in _SERVER_TO_CLIENT_METHODS:
            raise ValueError(
                f"'{method}' is a server→client method; a client cannot invoke "
                f"it. Client-issuable methods: {', '.join(sorted(_CLIENT_METHODS))}."
            )
        if method.startswith('notifications/'):
            raise ValueError(
                f"'{method}' is a notification (fire-and-forget); murl issues "
                f"request methods only."
            )
        if method not in _CLIENT_METHODS and not allow_unknown_method:
            raise ValueError(
                f"Unknown method '{method}'. Known methods: "
                f"{', '.join(sorted(_CLIENT_METHODS))}. "
                f"Use --allow-unknown-method to send it anyway."
            )

        if method in _CLIENT_METHODS:
            params = build_params_for_method(method, name_or_uri, data)
        else:
            # Forced unknown method: best-effort param shaping — positional
            # becomes 'name', data passes through as params.
            params = dict(data)
            if name_or_uri is not None:
                params.setdefault('name', name_or_uri)
        return base_url, method, params, False

    # Legacy URL-as-method form: a single token carrying the method in its path.
    base_url, virtual_path = parse_url(args[0])
    method, params = map_virtual_path_to_method(virtual_path, data)
    return base_url, method, params, True


def parse_headers(header_flags: Tuple[str, ...]) -> Dict[str, str]:
    """Parse -H/--header flags into a dictionary."""
    headers = {}

    for header in header_flags:
        if ':' not in header:
            raise ValueError(f"Invalid header format: {header}. Expected 'Key: Value'")

        key, value = header.split(':', 1)
        headers[key.strip()] = value.strip()

    return headers


# --- Text-envelope unwrap, paginate, and config discovery ---

def unwrap_text_envelope(result: Any) -> Tuple[Any, bool]:
    """Unwrap an MCP {type:"text", text:"<json>"} response when possible.

    Many MCP tool servers stringify their response payload and wrap it in a
    single text content block. The caller almost always wants the parsed inner
    value. Returns (unwrapped, did_unwrap). Idempotent: a result that is not a
    single-text envelope (or whose text isn't JSON) is returned unchanged.
    """
    if not isinstance(result, list) or len(result) != 1:
        return result, False
    item = result[0]
    if not isinstance(item, dict) or item.get("type") != "text":
        return result, False
    text = item.get("text")
    if not isinstance(text, str):
        return result, False
    try:
        return json.loads(text), True
    except (json.JSONDecodeError, ValueError):
        return result, False


# Auth failures a server reports as a successful tool result. The MCP spec
# signals a failed tool call with isError and a third-party authorization need
# with URL elicitation; these rules cover servers that return the failure as an
# ordinary result instead (no isError). Only a whole value at the top level of
# the result counts. A nested field, a list item or a quoted word is content.
_TOOL_AUTH_CODES = frozenset({
    "unauthorized", "unauthenticated", "invalid_token", "token_expired",
    "auth_failed", "authentication_failed",
})
_TOOL_AUTH_STATUSES = frozenset({401, 403})
_AUTH_DETAIL_MAX = 300
# Plain text that is only a status line with its reason phrase:
# "401 Unauthorized", "HTTP/1.1 403 Forbidden", "HTTP 401". A bare number or a
# number followed by other words ("401 users migrated") is content.
_AUTH_STATUS_LINE = re.compile(
    r"HTTP(?:/[\d.]+)?\s+(?:401(?:\s+Unauthori[sz]ed)?|403(?:\s+Forbidden)?)"
    r"|401\s+Unauthori[sz]ed|403\s+Forbidden",
    re.IGNORECASE,
)


class McpContent(list):
    """A tools/call result's content blocks as the server sent them.

    The type marks a result that unwrap_text_envelope could not parse (text
    that is not JSON, or several blocks). detect_tool_auth_failure applies the
    object rule to each block whose text is a JSON object and the text rule to
    the rest. A parsed result that is a list is a plain list, and its items are
    content.
    """


def _auth_code(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip().lower() in _TOOL_AUTH_CODES:
        return value.strip()
    return None


def _auth_in_object(node: Dict[str, Any]) -> Optional[str]:
    err = node.get("error")
    hit = _auth_code(err) or _auth_code(node.get("code")) or _auth_code(node.get("status"))
    if hit is None and isinstance(err, dict):
        hit = _auth_code(err.get("code"))
    if hit is None:
        for key in ("status", "statusCode"):
            status = node.get(key)
            # An exact int: a float 401.0 compares equal to 401.
            if type(status) is int and status in _TOOL_AUTH_STATUSES:
                hit = f"status {status}"
                break
    if hit is None:
        return None
    for detail in (node.get("reason"), node.get("message"),
                   err.get("message") if isinstance(err, dict) else err):
        if isinstance(detail, str) and detail.strip() and detail.strip() != hit:
            return f"{hit}: {detail.strip()[:_AUTH_DETAIL_MAX]}"
    return hit


def _auth_in_text(text: str) -> Optional[str]:
    stripped = text.strip()
    if stripped.lower() in _TOOL_AUTH_CODES or _AUTH_STATUS_LINE.fullmatch(stripped):
        return stripped[:_AUTH_DETAIL_MAX]
    return None


def detect_tool_auth_failure(unwrapped: Any) -> Optional[str]:
    """Return a short description if a successful tool result reports an auth
    failure, otherwise None.

    A JSON object matches on a whole top-level value: `error`, `error.code`,
    `code` or `status` equal to an auth code (_TOOL_AUTH_CODES, any case), or an integer
    `status`/`statusCode` of 401 or 403. Text matches when it is only an auth
    code or a status line. McpContent applies these rules to each of its first
    50 text blocks. Everything else is content.
    """
    if isinstance(unwrapped, dict):
        return _auth_in_object(unwrapped)
    if isinstance(unwrapped, str):
        return _auth_in_text(unwrapped)
    if isinstance(unwrapped, McpContent):
        for block in unwrapped[:50]:
            text = block.get("text") if isinstance(block, dict) else None
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError, RecursionError):
                parsed = text
            if isinstance(parsed, dict):
                hit = _auth_in_object(parsed)
            elif isinstance(parsed, str):
                hit = _auth_in_text(parsed)
            else:
                hit = None
            if hit:
                return hit
    return None


def find_mcp_config(start_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Walk up from start_dir (default: CWD) looking for a .mcp.json file.

    Returns the parsed config dict, or None if no file is found or it fails to
    parse. Stops at the filesystem root or the user's home directory, whichever
    comes first.
    """
    cwd = os.path.abspath(start_dir or os.getcwd())
    home = os.path.expanduser("~")
    while True:
        candidate = os.path.join(cwd, ".mcp.json")
        if os.path.isfile(candidate):
            try:
                with open(candidate) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
        parent = os.path.dirname(cwd)
        if parent == cwd or cwd == home:
            return None
        cwd = parent


def mcp_config_defaults(base_url: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract OAuth defaults for base_url from a parsed .mcp.json.

    Matches the server entry whose `url` exactly equals base_url (after URL
    decoding both, since AgentCore ARNs are percent-encoded in practice).
    Trailing slashes are normalized; query strings are not — the caller's URL
    must include the same query as the config entry, otherwise no match.
    Returns at most {client_id, callback_port} for filling missing flags.
    """
    if not config:
        return {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    target = urllib.parse.unquote(base_url).rstrip("/")
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        entry_url = entry.get("url")
        if not isinstance(entry_url, str):
            continue
        decoded = urllib.parse.unquote(entry_url).rstrip("/")
        if decoded != target:
            continue
        oauth = entry.get("oauth") or {}
        out: Dict[str, Any] = {}
        if isinstance(oauth.get("clientId"), str):
            out["client_id"] = oauth["clientId"]
        cb = oauth.get("callbackPort")
        if isinstance(cb, int):
            out["callback_port"] = cb
        return out
    return {}


# Header a client replays to stay attached to an existing MCP session
# (MCP 2025-11-25 §Session Management). Defined locally rather than imported
# from the SDK so this contract doesn't shift if the SDK renames its constant.
MCP_SESSION_ID_HEADER = "mcp-session-id"

# Cap on pages followed when a list endpoint paginates via nextCursor.
_MAX_LIST_PAGES = 1000


class RequestValidationError(ValueError):
    """murl rejected the request itself: a missing parameter, a method or capability
    the server does not offer. Never an auth failure, so the 401 handler reports it
    as INVALID_ARGUMENT without probing the server or starting OAuth."""


class ToolOutcome(Exception):
    """A tools/call the server answered with a failure, not a result.

    Raised from within the MCP session so the CLI reports it as a structured
    error instead of returning content or partial pages. Carries the page where
    it was seen during pagination, if any. Never a transport auth failure, so
    the 401/403 handler re-raises it without starting OAuth.
    """

    def __init__(self, message: str, page: Optional[int] = None) -> None:
        super().__init__(message)
        self.page = page


class ToolAuthError(ToolOutcome):
    """A successful tool result that reports an auth failure (see
    detect_tool_auth_failure). `content` is the result as murl would print it
    on success, so a false match loses nothing."""

    def __init__(self, message: str, page: Optional[int] = None, content: Any = None) -> None:
        super().__init__(message, page)
        self.content = content


class ToolCallError(ToolOutcome):
    """A tool result with isError: true. `content` is the result as murl would
    print it on success."""

    def __init__(self, content: Any, page: Optional[int] = None) -> None:
        super().__init__("Tool reported an error", page)
        self.content = content


class AuthorizationRequired(ToolOutcome):
    """The server needs the user to complete URL elicitation(s), such as a
    third-party OAuth consent, before the tool can run. `elicitations` holds
    {url, host, message} dicts (see _elicitation)."""

    def __init__(self, message: str, elicitations: List[Dict[str, str]],
                 page: Optional[int] = None) -> None:
        super().__init__(message, page)
        self.elicitations = elicitations


class InputRequired(ToolOutcome):
    """The server asked for input murl cannot give: a form elicitation,
    sampling or roots. `methods` names the requests."""

    def __init__(self, methods: List[str], page: Optional[int] = None) -> None:
        super().__init__(
            f"Server requires input murl cannot provide: {', '.join(methods) or '(none given)'}", page)
        self.methods = methods


def _tool_outcome(exc: BaseException) -> Optional[ToolOutcome]:
    """The ToolOutcome in exc or in any ExceptionGroup nested inside it."""
    return next((e for e in _flatten_exceptions(exc) if isinstance(e, ToolOutcome)), None)


def _save_live_session(base_url: str, negotiated: Optional[str], cached: Optional[str],
                       protocol: Optional[str], identity: str) -> None:
    """Cache the session the server answered on: the one it issued, else the
    replayed one (it routed the request, so it is still live)."""
    effective = negotiated or cached
    if isinstance(effective, str) and effective:
        session_store.save_session(base_url, effective, protocol, identity)


async def _unwrap_outcome(coro):
    """Await coro; a ToolOutcome raised inside the session's task group leaves
    unwrapped, so every caller sees the ToolOutcome itself."""
    try:
        return await coro
    except Exception as exc:
        outcome = _tool_outcome(exc)
        if outcome is not None and outcome is not exc:
            raise outcome from None
        raise


# The server writes the URL and message, so murl passes on only what a person
# can safely open and read: an http(s) URL with no userinfo, whitespace or
# control characters, whose host is a plain ASCII hostname or IP address (a
# browser reads a "\\" in the host as "/", and an IDN host can imitate
# another), and bounded text.
_ELICITATION_MESSAGE_MAX = 300
_ELICITATION_URL_MAX = 2048
_ELICITATIONS_MAX = 10
_INPUT_METHOD_MAX = 100
_PLAIN_HOST = re.compile(r"[A-Za-z0-9.-]+|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")


def _elicitation(url: Any, message: Any) -> Optional[Dict[str, str]]:
    """{url, host, message} for one URL elicitation, or None when the URL is
    not one murl passes on (see above)."""
    # A correctly percent-encoded URL is ASCII: this also rejects invisible
    # format characters (U+202E, U+200B) that make a URL display misleadingly.
    if not isinstance(url, str) or len(url) > _ELICITATION_URL_MAX or not url.isascii():
        return None
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    if not _PLAIN_HOST.fullmatch(host):
        return None
    msg = message if isinstance(message, str) else ""
    return {"url": url, "host": host, "message": msg[:_ELICITATION_MESSAGE_MAX]}


def _url_elicitations(raw: Any) -> List[Dict[str, str]]:
    """{url, host, message} for each URL-mode elicitation in a -32042 error's
    data.elicitations list (see _elicitation for which are kept), at most
    _ELICITATIONS_MAX. An entry whose mode is set to anything but "url" is
    skipped."""
    found = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and e.get("mode", "url") == "url":
            el = _elicitation(e.get("url"), e.get("message"))
            if el is not None:
                found.append(el)
    return found[:_ELICITATIONS_MAX]


async def _call_tool(session, tool_name: str, arguments: Dict[str, Any], raw: bool,
                     page: Optional[int] = None):
    """session.call_tool, with the spec's failure signals raised as ToolOutcome.

    A -32042 error and an InputRequiredResult of URL elicitations raise
    AuthorizationRequired; any other InputRequiredResult raises InputRequired.
    Unless raw, isError: true raises ToolCallError. --raw returns the result
    as the server sent it, so only the two request-level signals apply.
    """
    try:
        result = await session.call_tool(tool_name, arguments, allow_input_required=True)
    except MCPError as e:
        if e.code != URL_ELICITATION_REQUIRED:
            raise
        data = e.data if isinstance(e.data, dict) else {}
        raise AuthorizationRequired(e.message[:_ELICITATION_MESSAGE_MAX],
                                    _url_elicitations(data.get("elicitations")), page) from e
    if isinstance(result, InputRequiredResult):
        requests = list((result.input_requests or {}).values())
        params = [getattr(r, "params", None) for r in requests]
        if params and all(getattr(p, "mode", None) == "url" for p in params):
            urls = [_elicitation(p.url, p.message) for p in params]
            # The server's text is in each elicitation's message: no server_message.
            raise AuthorizationRequired("",
                                        [u for u in urls if u is not None][:_ELICITATIONS_MAX], page)
        methods = sorted({str(getattr(r, "method", "?"))[:_INPUT_METHOD_MAX] for r in requests})
        raise InputRequired(methods[:_ELICITATIONS_MAX], page)
    if not raw and getattr(result, "is_error", False) is True:
        content = McpContent(dump_wire(c) for c in result.content)
        unwrapped, _ = unwrap_text_envelope(content)
        raise ToolCallError(unwrapped, page)
    return result


# Phrases that signal a dead/unknown MCP session across server implementations.
# MCP 2025-11-25 §Session Management mandates a 404 for an unknown session, but
# real servers also surface it as 400/410 or varied wording. This set is
# deliberately broad but KNOWN-INCOMPLETE: an unmatched rejection just skips the
# self-heal and surfaces the error (the safe direction — never wrong data).
_SESSION_GONE_PHRASES = (
    "session terminated",
    "session not found",
    "session id not found",
    "unknown session",
    "invalid session",
    "session expired",
    "no such session",
)


def _flatten_exceptions(exc: BaseException):
    """Yield ``exc`` and every member of any nested ExceptionGroup, depth-first.

    anyio nests one TaskGroup inside another, so a transport failure can sit two
    or more groups deep.
    """
    yield exc
    if isinstance(exc, ExceptionGroup):
        for sub in exc.exceptions:
            yield from _flatten_exceptions(sub)


def _is_session_terminated_error(exc: BaseException) -> bool:
    """True if the exception signals a dead/unknown MCP session.

    The Streamable HTTP transport surfaces a server's 404 by injecting a
    JSON-RPC error (code 32600, "Session terminated"); we also match a raw httpx
    404, a 400/410 whose message mentions a session, and the known phrasings
    above. anyio re-raises background-task failures as an ExceptionGroup, so we
    inspect members recursively.
    """
    for e in _flatten_exceptions(exc):
        text = str(e).lower()
        if any(phrase in text for phrase in _SESSION_GONE_PHRASES):
            return True
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        if status == 404:
            return True
        # A bare HTTP 404 (no JSON-RPC body) reaches us as
        # `MCPError(-32601, "Not Found")` — the SDK maps a body-less rejection to
        # METHOD_NOT_FOUND, so the code cannot distinguish "session reaped" from
        # "no such method" and only the reason phrase can. Match it explicitly
        # rather than widening on -32601, which a genuinely absent method also
        # returns: treating that as a dead session would retry a request the
        # server will never serve.
        if "not found" == text.strip():
            return True
        # 400 ("invalid session") / 410 (Gone) are reused for other failures
        # too, so only treat them as session-gone when the message says so.
        if status in (400, 410) and "session" in text:
            return True
    return False


def _identity_fingerprint(headers: Dict[str, str]) -> str:
    """Stable fingerprint of the request's credential identity.

    A cached session is bound to the bearer token it was negotiated under so a
    rotated or different credential never replays another identity's session —
    the session ID is server-side routing/context, and reuse must not outlive
    the identity (e.g. a CI step that swaps roles, or an externally rotated
    token that still authenticates so no 401 fires). The raw token is never
    persisted, only this hash. Empty string when unauthenticated (--no-auth).
    """
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    return hashlib.sha256(auth.encode()).hexdigest() if auth else ""


def _env_flag(value: Optional[str]) -> bool:
    """Interpret an environment variable as a boolean flag."""
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def dump_wire(model: Any) -> Any:
    """Serialize an SDK model the way MCP puts it on the wire.

    ``by_alias=True`` is the whole point: the SDK's Python fields are snake_case
    while the protocol's JSON keys are camelCase (`input_schema` vs
    `inputSchema`), and murl's stdout IS the protocol's shape — callers pipe it
    into jq and agents parse it. Dumping without the alias silently renames every
    key, which is a breaking change to murl's output contract rather than an
    internal detail.
    """
    return model.model_dump(mode='json', exclude_none=True, by_alias=True)


async def _collect_all(fetch_page, extract_items):
    """Paginate a list endpoint, returning all items across pages.

    List operations use cursor-based pagination. All pages run on the one open
    session bound into ``fetch_page``. Guards against buggy servers that repeat
    or cycle cursors.

    The cursor rides in a ``PaginatedRequestParams`` rather than a ``cursor=``
    keyword: the SDK moved every list method to a single ``params`` argument, and
    passing ``cursor=`` raises ``TypeError``. The first page passes ``None`` so
    the request carries no params object at all.
    """
    all_items = []
    params = None
    seen_cursors: set = set()
    for _ in range(_MAX_LIST_PAGES):
        result = await fetch_page(params=params)
        all_items.extend(extract_items(result))
        # End of pagination is signalled by an absent cursor. Per MCP, the cursor
        # is an opaque string, so test `is None` — `not cursor` would also stop on
        # a falsy-but-valid cursor like "".
        next_cursor = result.next_cursor
        if next_cursor is None or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        params = PaginatedRequestParams(cursor=next_cursor)
    return [dump_wire(item) for item in all_items]


async def _follow_tool_pagination(session, tool_name, base_arguments,
                                  first_content, max_pages, verbose):
    """Unwrap a tools/call envelope and follow body-level nextCursor pagination
    on the SAME open session.

    Tool servers commonly stringify a ``{<items>: [...], hasMore, nextCursor}``
    payload into a single text block. When more pages are reported, we re-call
    the same tool with ``cursor`` set — every page on one session, one
    initialize, rather than a fresh session (and, on AgentCore, a fresh
    microVM) per page. Returns the aggregated items list (paginated), the
    single unwrapped object (single page / ambiguous shape), or the raw content
    (non-envelope).
    """
    unwrapped, did_unwrap = unwrap_text_envelope(first_content)
    if not did_unwrap:
        return first_content
    if not isinstance(unwrapped, dict):
        return unwrapped

    # A tool that reports more pages but supplies no cursor leaves us no way to
    # fetch them — warn so the truncation is visible rather than masquerading
    # as a complete result.
    # nextCursor is an opaque string (MCP): test `is None`, not truthiness, so a
    # valid empty-string cursor isn't read as "no more pages" — mirrors
    # _collect_all and the 0.6.0 list-pagination fix.
    if unwrapped.get("hasMore") and unwrapped.get("nextCursor") is None:
        click.echo(
            "Warning: tool response reports hasMore=true but provided no "
            "nextCursor; results may be incomplete.",
            err=True,
        )

    if not unwrapped.get("hasMore") or unwrapped.get("nextCursor") is None:
        return unwrapped

    # Locate the single array field carrying page items. The MCP convention is
    # {<items_field>: [...], hasMore, nextCursor}.
    array_keys = [k for k, v in unwrapped.items() if isinstance(v, list)]
    if len(array_keys) != 1:
        # Ambiguous page shape — surface the unwrapped envelope as-is.
        return unwrapped

    items_key = array_keys[0]
    all_items = list(unwrapped[items_key])
    seen_cursors: set = set()
    cursor = unwrapped.get("nextCursor")
    pages = 1
    while (unwrapped.get("hasMore") and cursor is not None
           and cursor not in seen_cursors and pages < max_pages):
        seen_cursors.add(cursor)
        next_arguments = {**base_arguments, 'cursor': cursor}
        # Strip page-size hints on cursor calls — many MCP tools reject them
        # when a cursor is supplied because the cursor already encodes that
        # context.
        for k in ('pageSize', 'perPage', 'status'):
            next_arguments.pop(k, None)
        next_result = await _call_tool(session, tool_name, next_arguments, False, page=pages + 1)
        next_content = McpContent(dump_wire(c) for c in next_result.content)
        next_unwrapped, _ = unwrap_text_envelope(next_content)
        # Detect a mid-pagination auth failure before deciding whether to break:
        # an error body looks structurally like an "end of pagination" body (no
        # items_key), and silently breaking would return partial results with no
        # signal that auth expired mid-loop.
        page_failure = detect_tool_auth_failure(next_unwrapped)
        if page_failure:
            # The match is a heuristic: keep the pages already collected too.
            raise ToolAuthError(page_failure, page=pages + 1, content=all_items + [next_unwrapped])
        if not isinstance(next_unwrapped, dict) or items_key not in next_unwrapped:
            break
        all_items.extend(next_unwrapped.get(items_key, []))
        cursor = next_unwrapped.get("nextCursor")
        unwrapped = next_unwrapped
        pages += 1
    return all_items


class _Negotiation(NamedTuple):
    """How a session was established, and what the server told us."""

    #: "2026-07-28" or the 2025-era version string the handshake settled on.
    protocol_version: Optional[str]
    #: Server capabilities, from whichever path succeeded.
    capabilities: Any
    #: True when `server/discover` succeeded (no `initialize` handshake ran).
    modern: bool


async def _initialize_or_reraise(session, probe_error: Optional[BaseException]):
    """Run the `initialize` handshake, surfacing the probe's error if it also fails.

    The point is which cause a caller sees. When `server/discover` failed for a
    real reason — an expired token, an unreachable host — the handshake fails the
    same way, and reporting the handshake's error would hide the first and more
    informative one. When the probe failed only because the server does not
    implement `server/discover`, the handshake succeeds and the probe error is
    correctly discarded.

    The probe error is chained as `__cause__` rather than swallowed, so a
    traceback shows both attempts.
    """
    try:
        return await session.initialize()
    except Exception as init_error:
        if probe_error is not None:
            raise probe_error from init_error
        raise


async def _negotiate_era(session, verbose: bool) -> _Negotiation:
    """Establish the session against either MCP protocol revision.

    Tries `server/discover` first (the 2026-07-28 entry point) and falls back to
    the `initialize` handshake. The SDK deliberately leaves this policy to the
    caller: `discover()` propagates every error other than a version mismatch,
    including the `METHOD_NOT_FOUND` a 2025-era server returns because it has no
    `server/discover` at all.

    Discover-first rather than handshake-first: a 2026-07-28 server rejects
    `initialize` outright, so leading with the handshake would fail against
    exactly the servers this revision exists for. A 2025-era server, by
    contrast, answers the probe with an error we can recover from — so the
    ordering costs one extra round trip against old servers and works against
    both, where the reverse only works against old ones.

    Older SDKs have no `discover()`; there the legacy path is the only path.
    """
    # Holds the probe's failure so the handshake can re-raise it rather than
    # replacing it. None when the probe never ran or never failed.
    probe_error: Optional[BaseException] = None

    discover = getattr(session, "discover", None)
    if discover is not None:
        try:
            result = await discover()
            # Require a real DiscoverResult rather than trusting truthiness. A
            # duck-typed stand-in — an AsyncMock in a test, a partial object from
            # a future SDK — would otherwise be accepted as a successful probe and
            # skip the handshake, hiding whatever the legacy path would have said.
            if not isinstance(result, DiscoverResult):
                raise TypeError(
                    f"server/discover returned {type(result).__name__}, not DiscoverResult"
                )
            if verbose:
                click.echo("=== MCP server/discover ===", err=True)
                click.echo(f"Supported Versions: {', '.join(result.supported_versions)}", err=True)
                click.echo("", err=True)
            # `supported_versions` is what the server offers; the session adopts
            # the highest mutual one, which is the newest it listed that we know.
            return _Negotiation(
                protocol_version=result.supported_versions[0] if result.supported_versions else None,
                capabilities=result.capabilities,
                modern=True,
            )
        except Exception as exc:  # noqa: BLE001 — deliberate; see below
            # ANY probe failure falls through to the handshake, but the original
            # exception is preserved and re-raised if the handshake ALSO fails.
            #
            # A narrower gate was tried first and does not work. The era signal a
            # server sends is not what the client receives: Amazon Bedrock
            # AgentCore collapses a runtime-side
            # JSON-RPC error into a generic 400, and the code the SDK surfaces for
            # one unchanged "unknown method" condition was measured as -32601
            # (direct), -32603 (unauthenticated) and -32010 "Received error (400)
            # from runtime" (authenticated). Any allowlist of proxy codes is a
            # list that goes stale the next time a hosting layer is added, and
            # while it is stale murl cannot reach those servers at all.
            #
            # Falling back unconditionally costs one extra round trip when the
            # failure is genuine. Re-raising the probe error when the handshake
            # also fails is what keeps that cheap: a real fault (auth, transport,
            # a client bug) still surfaces its true cause rather than a
            # misleading handshake error, which is the property the narrow gate
            # existed to protect.
            probe_error = exc
            if verbose:
                click.echo(f"server/discover unavailable ({exc}); falling back to initialize", err=True)

    init_result = await _initialize_or_reraise(session, probe_error)
    version = init_result.protocol_version
    server_info = init_result.server_info
    if verbose:
        click.echo("=== MCP Initialization ===", err=True)
        click.echo(f"Protocol Version: {version}", err=True)
        if server_info is not None:
            click.echo(f"Server: {server_info.name} {server_info.version}", err=True)
        click.echo("", err=True)
    return _Negotiation(
        protocol_version=str(version) if version is not None else None,
        capabilities=init_result.capabilities,
        modern=False,
    )


async def _run_mcp_session(base_url, method, params, headers, verbose,
                           raw, max_pages, session_reuse, cached_session_id, identity):
    """Open one MCP session and run the request on it, returning the processed
    response.

    All work for one logical request — including any tools/call body-level
    pagination — happens on this single ClientSession (one initialize). When
    ``session_reuse`` is on, ``cached_session_id`` (if any) is replayed via the
    Mcp-Session-Id header so the server keeps us on the same session, the
    session is left open on close (``terminate_on_close=False``), and the
    negotiated session ID is persisted for the next invocation.
    """
    # Create httpx client with custom headers and reasonable timeout
    import httpx
    client_headers = dict(headers or {})
    if cached_session_id:
        # Replay the cached session ID. On the initialize request the SDK's
        # transport has no session_id yet, so it does not set this header and
        # our client-level value passes through; subsequent same-session
        # requests carry the negotiated value (identical, so no conflict).
        client_headers[MCP_SESSION_ID_HEADER] = cached_session_id

    # Capture the server-issued session id off the response headers.
    #
    # The SDK transport no longer hands one back: on the 2026-07-28 revision
    # protocol sessions are gone, so `streamable_http_client` yields only
    # (read, write) and the old `get_session_id` accessor went with them. A
    # 2025-era server still issues the `Mcp-Session-Id` header, and murl already
    # owns this httpx client — so read it where it actually arrives. Sessionless
    # servers simply never set it, leaving this None, which is the correct answer
    # for them rather than an error.
    observed_session_id: Dict[str, str] = {}

    async def _capture_session_id(response: "httpx.Response") -> None:
        value = response.headers.get(MCP_SESSION_ID_HEADER)
        if value:
            observed_session_id["value"] = value

    http_client = httpx.AsyncClient(
        headers=client_headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
        event_hooks={"response": [_capture_session_id]},
    )

    # A reused session must NOT be torn down on close — the whole point is to
    # leave the server-side session alive for the next invocation to attach to
    # (also avoids the spurious 404-on-DELETE for stateless AgentCore servers).
    terminate_on_close = not session_reuse

    # Sentinel distinguishing "no response yet" from a legitimate response of
    # None/empty. Once a response is produced, a late exception raised during
    # the SDK's stream/session teardown (anyio task-group __aexit__) must NOT
    # escape and clobber a successful result.
    _NO_RESPONSE = object()
    response = _NO_RESPONSE
    negotiated_session_id: Optional[str] = None
    negotiated_protocol: Optional[str] = None
    negotiation: Optional[_Negotiation] = None

    try:
        try:
            async with streamable_http_client(
                base_url, http_client=http_client, terminate_on_close=terminate_on_close
            ) as streams:
                # The transport yields exactly (read, write): protocol sessions
                # are gone from the 2026-07-28 revision, so the old
                # `get_session_id` accessor went with them. The session id, when a
                # 2025-era server issues one, is captured from the response
                # headers by the hook above.
                read, write = streams

                async with ClientSession(read, write) as session:
                    negotiation = await _negotiate_era(session, verbose)
                    capabilities = negotiation.capabilities

                    # Both revisions: only use capabilities the server advertised.
                    # `server/discover` and `initialize` both return them, so this
                    # check is era-independent.
                    category = method.split('/')[0]
                    if category in ("tools", "resources", "prompts"):
                        if getattr(capabilities, category, None) is None:
                            available = [
                                cap for cap in ["tools", "resources", "prompts", "logging", "completions"]
                                if getattr(capabilities, cap, None) is not None
                            ]
                            raise RequestValidationError(
                                f"Server does not support '{category}'. Supported: {', '.join(available)}"
                            )

                    # Compute the response INSIDE the session context but store it
                    # rather than returning directly. Returning here would run the
                    # context-manager teardown on the way out of the function; by
                    # storing and letting the `async with` blocks exit normally we
                    # can isolate any teardown-time exception below (#19).
                    if method == 'tools/list':
                        response = await _collect_all(session.list_tools, lambda r: r.tools)
                    elif method == 'tools/call':
                        tool_name = params.get('name')
                        arguments = params.get('arguments', {})
                        result = await _call_tool(session, tool_name, arguments, raw)
                        content = McpContent(dump_wire(c) for c in result.content)
                        # tools/call body-level pagination runs on THIS session.
                        # --raw opts out of unwrap + cursor-follow entirely.
                        if raw:
                            response = content
                        else:
                            response = await _follow_tool_pagination(
                                session, tool_name, arguments, content, max_pages, verbose
                            )
                    elif method == 'resources/list':
                        response = await _collect_all(session.list_resources, lambda r: r.resources)
                    elif method == 'resources/templates/list':
                        response = await _collect_all(
                            session.list_resource_templates,
                            lambda r: r.resource_templates
                        )
                    elif method == 'resources/read':
                        uri = params.get('uri')
                        result = await session.read_resource(uri)
                        response = [dump_wire(content) for content in result.contents]
                    elif method == 'prompts/list':
                        response = await _collect_all(session.list_prompts, lambda r: r.prompts)
                    elif method == 'prompts/get':
                        prompt_name = params.get('name')
                        arguments = params.get('arguments', {})
                        result = await session.get_prompt(prompt_name, arguments)
                        response = [dump_wire(message) for message in result.messages]
                    else:
                        raise RequestValidationError(f"Unsupported method: {method}")

                    # Record what the server negotiated, for the next invocation.
                    # A 2026-07-28 server issues no session id (the revision has
                    # no protocol sessions), so this stays None there and nothing
                    # is cached — correct, not a failure.
                    if session_reuse:
                        negotiated_session_id = observed_session_id.get("value")
                        if negotiation.protocol_version:
                            negotiated_protocol = negotiation.protocol_version
        except Exception as exc:
            # Only reached when an exception propagated out of the `async with`
            # blocks. Two cases:
            #
            #   1. A response was already produced — the exception was raised
            #      while tearing down the MCP stream/session task group AFTER a
            #      successful exchange (anyio re-raises a failed background task
            #      as an ExceptionGroup from __aexit__). Swallow it so a completed
            #      request still returns cleanly with exit 0 — surfacing it only
            #      under -v. This is the #19 fix.
            #
            #   2. No response was produced — this is the real failure (auth,
            #      transport, tool error, dead session), possibly with teardown
            #      noise chained onto it. Re-raise so the caller's handling sees it.
            #
            # We catch Exception (not BaseException) so KeyboardInterrupt,
            # SystemExit, and asyncio.CancelledError always propagate. anyio's
            # ExceptionGroup is an Exception subclass, so the #19 case is still
            # caught.
            if response is _NO_RESPONSE:
                # A tool outcome means the server answered on this session:
                # cache it as a successful call would, then report the outcome.
                if session_reuse and _tool_outcome(exc) is not None:
                    _save_live_session(
                        base_url, observed_session_id.get("value"), cached_session_id,
                        negotiation.protocol_version if negotiation else None, identity,
                    )
                raise
            if verbose:
                click.echo(
                    f"Note: ignored a post-response teardown exception "
                    f"({type(exc).__name__}): {exc}",
                    err=True,
                )

        # Persist the negotiated (or replayed) session ID for the next
        # invocation. If the server didn't echo one back, keep replaying the
        # cached value — it routed us correctly, so it's still the live session.
        if session_reuse:
            _save_live_session(base_url, negotiated_session_id, cached_session_id,
                               negotiated_protocol, identity)

        return response
    finally:
        if http_client is not None:
            await http_client.aclose()


async def make_mcp_request(
    base_url: str,
    method: str,
    params: Dict[str, Any],
    headers: Dict[str, str],
    verbose: bool,
    *,
    raw: bool = False,
    max_pages: int = 1000,
    session_reuse: bool = False,
) -> Any:
    """Make an MCP request using the official SDK, on a single session.

    All work for one logical request — including any tools/call body-level
    pagination — runs on one ClientSession (one initialize), rather than a new
    session per page. When ``session_reuse`` is on, a cached Mcp-Session-Id is
    replayed so the server keeps us on the same session across invocations; a
    server-rejected (stale) session is discarded and renegotiated once.
    """
    # Validate required parameters before opening a connection.
    if method == 'tools/call':
        if params.get('name') is None:
            raise RequestValidationError("Missing required 'name' parameter for tools/call method")
    elif method == 'resources/read':
        if params.get('uri') is None:
            raise RequestValidationError("Missing required 'uri' parameter for resources/read method")
    elif method == 'prompts/get':
        if params.get('name') is None:
            raise RequestValidationError("Missing required 'name' parameter for prompts/get request")

    if verbose:
        click.echo("=== MCP Request ===", err=True)
        click.echo(f"Method: {method}", err=True)
        click.echo(f"Params: {json.dumps(params, indent=2)}", err=True)
        click.echo(f"URL: {base_url}", err=True)
        if headers:
            click.echo(f"Headers: {json.dumps(headers, indent=2)}", err=True)
        click.echo("", err=True)

    # Bind reuse to the credential identity so a rotated/different token never
    # replays another identity's session (see _identity_fingerprint).
    identity = _identity_fingerprint(headers)
    cached_session_id = (
        session_store.get_session_id(base_url, identity) if session_reuse else None
    )

    try:
        return await _unwrap_outcome(_run_mcp_session(
            base_url, method, params, headers, verbose,
            raw, max_pages, session_reuse, cached_session_id, identity,
        ))
    except Exception as exc:
        # The server answered the call: never retry it, whatever its text says.
        if isinstance(exc, ToolOutcome):
            raise
        # A replayed session ID the server no longer recognizes (404 / "Session
        # terminated" and friends, MCP 2025-11-25 §Session Management) is not a
        # real error — discard it and renegotiate a fresh session once.
        # Genuine failures propagate unchanged.
        if cached_session_id and _is_session_terminated_error(exc):
            session_store.clear_session(base_url)
            if verbose:
                click.echo(
                    "Cached MCP session was rejected by the server; "
                    "starting a fresh session...",
                    err=True,
                )
            return await _unwrap_outcome(_run_mcp_session(
                base_url, method, params, headers, verbose,
                raw, max_pages, session_reuse, None, identity,
            ))
        raise


def _emit_result(result: Any, verbose: bool, output_format: str) -> None:
    """Print a result to stdout in the selected format."""
    if verbose:
        click.echo(json.dumps(result, indent=2))
    elif output_format == 'toon':
        if toon_encode is None:
            output_error(
                error_type="MISSING_DEPENDENCY",
                message="python-toon package is required for --format toon",
                exit_code=ErrorCode.GENERAL_ERROR,
                suggestion='Install it with: pip install "mcp-curl[toon]"'
            )
            return
        click.echo(toon_encode(result))
    elif isinstance(result, list):
        for item in result:
            click.echo(json.dumps(item, separators=(',', ':')))
    else:
        click.echo(json.dumps(result, separators=(',', ':')))


_TOOL_ERROR_TEXT_MAX = 500


def _tool_error_text(content: Any) -> str:
    """The tool's error text for the TOOL_ERROR message: its text blocks
    joined, or the content as JSON, cut to _TOOL_ERROR_TEXT_MAX characters.
    The full content is on stdout."""
    blocks = content if isinstance(content, list) else [content]
    texts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
    text = " ".join(texts) if texts else json.dumps(content, separators=(',', ':'))
    return text if len(text) <= _TOOL_ERROR_TEXT_MAX else text[:_TOOL_ERROR_TEXT_MAX] + "..."


def _emit_outcome_content(content: Any, verbose: bool, output_format: str) -> None:
    """Print a failed call's content as on success. Without python-toon it
    falls back to JSON, so the error after it is still reported."""
    fmt = 'json' if output_format == 'toon' and toon_encode is None else output_format
    _emit_result(content, verbose, fmt)


def _report_tool_outcome(e: ToolOutcome, verbose: bool, output_format: str) -> None:
    """Report a ToolOutcome as a structured error on stderr and exit 1."""
    at_page = f" at page {e.page}" if e.page is not None else ""
    if isinstance(e, AuthorizationRequired):
        if e.elicitations:
            urls = ", ".join(el["url"] for el in e.elicitations)
            message = f"Server requires authorization{at_page}. Open: {urls}"
            suggestion = ("The server supplied this URL: check its host, open it in a browser, "
                          "complete the authorization, then re-run the same command. murl's own "
                          "login is unchanged.")
        else:
            message = f"Server requires authorization{at_page}. It gave no usable URL."
            suggestion = ("The server asked for third-party authorization without an http(s) "
                          "link murl can pass on. Authorize the account with the server another "
                          "way, then re-run. murl's own login is unchanged.")
        output_error(
            error_type="AUTHORIZATION_REQUIRED",
            message=message,
            exit_code=ErrorCode.GENERAL_ERROR,
            suggestion=suggestion,
            # The server's own prose stays in its own field, away from the
            # vetted URLs: it is free text and may name a URL murl withheld.
            extra={"elicitations": e.elicitations, **({"server_message": str(e)} if str(e) else {})},
        )
    elif isinstance(e, InputRequired):
        output_error(
            error_type="INPUT_REQUIRED",
            message=f"{e}{at_page}",
            exit_code=ErrorCode.GENERAL_ERROR,
            suggestion="Use an interactive MCP client for this tool.",
            extra={"requests": e.methods},
        )
    elif isinstance(e, ToolCallError):
        # The content is the tool's own explanation: print it as on success.
        _emit_outcome_content(e.content, verbose, output_format)
        output_error(
            error_type="TOOL_ERROR",
            message=f"Tool reported an error (isError: true){at_page}: {_tool_error_text(e.content)}",
            exit_code=ErrorCode.GENERAL_ERROR,
        )
    elif isinstance(e, ToolAuthError):
        # The match is a heuristic: print the result too, so a false match
        # loses nothing.
        if e.content is not None:
            _emit_outcome_content(e.content, verbose, output_format)
        output_error(
            error_type="AUTH_FAILED",
            message=f"Tool reported authentication failure{at_page}: {e}",
            exit_code=ErrorCode.GENERAL_ERROR,
            suggestion="If murl's own token expired, run `murl --login <url>`. If the "
                       "server's third-party account is not authorized, authorize it with "
                       "the server, then retry.",
        )
    else:
        output_error(
            error_type="TOOL_ERROR",
            message=f"Tool call failed{at_page}: {e}",
            exit_code=ErrorCode.GENERAL_ERROR,
        )


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


# Upgrade with the optional extras so an upgrade keeps keychain storage and
# TOON output working for users who installed them.
UPGRADE_SOURCE = "mcp-curl[keychain,toon]"


def run_upgrade(ctx, param, value):
    """Run the upgrade process."""
    if not value or ctx.resilient_parsing:
        return

    click.echo("Upgrading murl...")

    def show_error_and_exit(error_msg: str):
        click.echo(f"Error: {error_msg}", err=True)
        click.echo(f'Please try upgrading manually with: pip install --upgrade "{UPGRADE_SOURCE}"', err=True)
        ctx.exit(1)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", UPGRADE_SOURCE],
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
  (the second argument is the MCP method)
  murl $S tools/list                              # List tools
  murl $S tools/call -d name=echo -d message=hi   # Call a tool
  murl $S tools/call echo -d message=hi           # ...name as positional sugar
  murl $S tools/call -d name=search -d limit:=5   # Raw JSON value (number)
  murl $S resources/list                          # List resources
  murl $S resources/read -d uri=file:///a.txt     # Read a resource (or: resources/read <uri>)
  murl $S prompts/get -d name=greeting            # Get a prompt
  echo '{"message":"hi"}' | murl $S tools/call -d name=echo -d @-   # Args from stdin

  Legacy (0.5.x, deprecated — method in the URL path):
  murl https://server.com/mcp/tools/echo -d message=hello

PIPELINES:
  murl $S/tools/search -d q=foo | jq '{id:.[0].text}' | murl $S/tools/get -d @-

AUTHENTICATION:
  OAuth 2.0 with PKCE is built in (Dynamic Client Registration or pre-configured).
  Credentials auto-refresh. On 401, re-authenticates and retries once.

  murl --login https://server.com/mcp/tools    # First-time auth (opens browser)
  murl https://server.com/mcp/tools            # Uses stored token
  murl --no-auth https://server.com/mcp/tools  # Skip auth
  murl -H "Authorization: Bearer <tok>" <url>  # Manual token

  Pre-configured OAuth:
  murl --client-id ID --client-secret SECRET --callback-port 8080 --scope openid <url>

  Credentials: ~/.murl/credentials/<hash>.json

OPTIONS:
  -d, --data <val>             Request data: key=value, key:=rawjson, JSON, @file, @- (repeatable)
  -H, --header <Key: Value>    HTTP header (repeatable)
  -v, --verbose                Pretty-print output, show request debug info
  --format <json|toon>          Output format (default: json, toon for LLMs)
  --login                      Force OAuth re-authentication
  --no-auth                    Skip all authentication
  --client-id <id>             Pre-registered OAuth client ID (skip DCR)
  --client-secret <secret>     Pre-registered OAuth client secret (or MURL_CLIENT_SECRET env)
  --callback-port <port>       Fixed port for OAuth callback redirect URI
  --scope <scopes>             OAuth scope to request (e.g. "openid profile")
  --raw                        Skip auto-unwrap and cursor follow; emit the protocol response verbatim
  --max-pages <N>              Cap auto-paginated tools/call follow at N pages (default 1000)
  --allow-unknown-method       Send a method not in murl's known set (forward-compat)
  --no-mcp-config              Skip .mcp.json discovery for OAuth client defaults
  --no-session                 Don't reuse/persist the MCP session (fresh Mcp-Session-Id each call)
  --version                    Version info
  --upgrade                    Self-upgrade via pip
  -h, --help                   This help

METHODS (second argument — the literal MCP JSON-RPC method):
  tools/list                 prompts/list           resources/list
  tools/call -d name=...      prompts/get -d name=.. resources/read -d uri=...
  resources/templates/list
  Server→client methods (sampling/createMessage, roots/list, elicitation/create)
  cannot be invoked by a client. Use --allow-unknown-method for other methods
  (e.g. completion/complete, logging/setLevel) not yet handled natively.

  The resources/read URI: pass -d uri=<uri> or a positional <uri>. A bare path is
  coerced to file://<path>; pass a full scheme (https://, file://) to override.

OUTPUT:
  stdout  Compact JSON (NDJSON for lists). Pretty-printed with -v. TOON with --format toon.
  stderr  Errors as {"error":"CODE","message":"...","code":N}
  exit    0=success  1=error  2=invalid args"""
    click.echo(help_text)
    ctx.exit()


def _probe_www_authenticate(base_url: str) -> Optional[str]:
    """Send an unauthenticated POST to the MCP endpoint to elicit a 401.

    Per MCP 2025-11-25 §Protected Resource Metadata Discovery Requirements,
    the server MUST include the resource_metadata URL in the WWW-Authenticate
    header on 401 responses.  For servers with complex URLs (encoded ARNs,
    query parameters), well-known URI construction from the URL alone may fail,
    so this probe ensures we get the server-provided discovery URL.

    Returns the WWW-Authenticate header value, or None if the probe fails.
    """
    try:
        resp = _httpx.post(
            base_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
        )
        if resp.status_code == 401:
            return resp.headers.get("www-authenticate")
    except _httpx.HTTPError:
        pass
    return None


@click.command(context_settings={"allow_interspersed_args": True})
@click.argument('args', nargs=-1)
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
@click.option('--format', 'output_format', type=click.Choice(['json', 'toon']), default='json',
              help='Output format (default: json, toon = token-efficient for LLMs)')
@click.option('--login', is_flag=True, help='Force OAuth re-authentication')
@click.option('--no-auth', is_flag=True, help='Skip all authentication')
@click.option('--client-id', default=None, help='Pre-registered OAuth client ID (skips dynamic registration)')
@click.option('--client-secret', default=None, envvar='MURL_CLIENT_SECRET',
              help='Pre-registered OAuth client secret (or set MURL_CLIENT_SECRET)')
@click.option('--callback-port', default=None, type=int,
              help='Fixed port for OAuth callback (must match registered redirect URI)')
@click.option('--scope', default=None,
              help='OAuth scope to request (e.g. "openid profile")')
@click.option('--raw', is_flag=True,
              help='Skip auto-unwrap and auto-paginate; emit the protocol response verbatim')
@click.option('--max-pages', default=1000, type=int, show_default=True,
              help='Maximum pages to follow when a tools/call response carries nextCursor')
@click.option('--allow-unknown-method', is_flag=True,
              help='Send a method not in murl\'s known client-issuable set (forward-compat)')
@click.option('--no-mcp-config', is_flag=True,
              help='Skip .mcp.json discovery for OAuth client defaults')
@click.option('--no-session', is_flag=True,
              help='Disable MCP session reuse: negotiate a fresh Mcp-Session-Id '
                   'each call and don\'t persist it (or set MURL_NO_SESSION)')
def main(args: Tuple[str, ...], data_flags: Tuple[str, ...], header_flags: Tuple[str, ...],
         verbose: bool, output_format: Optional[str], login: bool, no_auth: bool,
         client_id: Optional[str], client_secret: Optional[str],
         callback_port: Optional[int], scope: Optional[str],
         raw: bool, max_pages: int, allow_unknown_method: bool, no_mcp_config: bool,
         no_session: bool):
    """murl - MCP Curl"""
    if not args:
        output_error(
            error_type="MISSING_ARGUMENT",
            message="URL argument is required",
            exit_code=ErrorCode.INVALID_ARGUMENT,
            suggestion="Run: murl --help"
        )

    if output_format != 'json' and verbose:
        output_error(
            error_type="INVALID_ARGUMENT",
            message="--format and --verbose are mutually exclusive",
            exit_code=ErrorCode.INVALID_ARGUMENT,
            suggestion="Use --format for alternative output or -v for verbose JSON, not both"
        )

    try:
        data = parse_data_flags(data_flags) if data_flags else {}
        base_url, method, params, used_legacy = resolve_invocation(
            args, data, allow_unknown_method=allow_unknown_method
        )
        # Soft-deprecate the 0.5.x URL-as-method form. Emit only under -v and
        # only to stderr (structured) so pipelines reading stdout are untouched.
        # Target removal: 1.0. See issue #14.
        if used_legacy and verbose:
            click.echo(json.dumps({
                "warning": "DEPRECATED_SURFACE",
                "message": ("URL-as-method form is deprecated; pass the method as "
                            "the second argument, e.g. `murl <url> tools/call -d name=...`."),
                "surface": "url-as-method",
            }), err=True)
        headers = parse_headers(header_flags) if header_flags else {}

        # --- .mcp.json defaults ---
        # When the caller didn't pass --client-id / --callback-port, look for a
        # sibling .mcp.json (walking up from CWD) and pick them up from the
        # matching server entry. Mirrors Claude Code's discovery semantics.
        if not no_mcp_config:
            cfg_defaults = mcp_config_defaults(base_url, find_mcp_config())
            if client_id is None and "client_id" in cfg_defaults:
                client_id = cfg_defaults["client_id"]
                if verbose:
                    click.echo(f"Using client_id from .mcp.json: {client_id}", err=True)
            if callback_port is None and "callback_port" in cfg_defaults:
                callback_port = cfg_defaults["callback_port"]
                if verbose:
                    click.echo(f"Using callback_port from .mcp.json: {callback_port}", err=True)

        # --- Auth ---
        # Common kwargs for all authorize() calls
        auth_kwargs = {}
        if client_id:
            auth_kwargs["client_id"] = client_id
        if client_secret:
            auth_kwargs["client_secret"] = client_secret
        if callback_port:
            auth_kwargs["callback_port"] = callback_port
        if scope:
            auth_kwargs["scope"] = scope

        has_auth_header = any(k.lower() == 'authorization' for k in headers)
        if not no_auth and not has_auth_header:
            if login:
                clear_credentials(base_url)

            creds = get_credentials(base_url)

            if creds and not login:
                if is_expired(creds):
                    try:
                        creds = refresh_token(creds)
                        save_credentials(base_url, creds)
                    except (OAuthError, _httpx.HTTPError, KeyError) as refresh_err:
                        # Refresh can fail for reasons beyond OAuthError: a
                        # network error / timeout from the token endpoint
                        # (httpx.HTTPError) or a malformed token response missing
                        # expected fields (KeyError). In every case the recovery
                        # is the same — fall back to a full re-authorization
                        # rather than crashing with an unhandled exception.
                        if verbose:
                            click.echo(
                                f"Token refresh failed ({type(refresh_err).__name__}); "
                                f"falling back to re-authentication.",
                                err=True,
                            )
                        # Probe for WWW-Authenticate so discovery has the
                        # resource_metadata URL (needed for complex server URLs).
                        www_auth = _probe_www_authenticate(base_url)
                        creds = authorize(base_url, www_authenticate=www_auth, **auth_kwargs)
                        save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
            elif login:
                # Probe for WWW-Authenticate so discovery has the
                # resource_metadata URL (needed for complex server URLs).
                www_auth = _probe_www_authenticate(base_url)
                creds = authorize(base_url, www_authenticate=www_auth, **auth_kwargs)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"

        # --- MCP session reuse ---
        # Replay a cached Mcp-Session-Id across invocations so AgentCore (and
        # any Streamable HTTP server) keeps us pinned to one runtime session
        # instead of provisioning a fresh one — and, on AgentCore, a fresh
        # microVM — per request. On by default; opt out with --no-session or
        # MURL_NO_SESSION. A fresh --login starts a fresh session.
        session_reuse = not no_session and not _env_flag(os.environ.get("MURL_NO_SESSION"))
        if login and session_reuse:
            session_store.clear_session(base_url)

        # --- Request with 401 retry ---
        try:
            result = asyncio.run(make_mcp_request(
                base_url, method, params, headers, verbose,
                raw=raw, max_pages=max_pages, session_reuse=session_reuse,
            ))
        except ToolOutcome:
            # A tool-level failure (incl. mid-pagination) is not a transport
            # 401 — let it fall through to its structured handler rather than
            # triggering the OAuth re-auth flow below.
            raise
        except (Exception, ExceptionGroup) as req_err:
            # Extract WWW-Authenticate header and 401 status from the exception chain.
            # The MCP SDK raises httpx.HTTPStatusError (possibly wrapped in ExceptionGroup)
            # which carries the full HTTP response including headers.
            is_401 = False
            is_403 = False
            www_auth_401 = None
            www_auth_403 = None
            transport_failed = False
            local_rejection = False

            # Inspect every nested exception before choosing a branch: a 403
            # listed ahead of a 401 must not hide the 401's OAuth challenge.
            for exc in _flatten_exceptions(req_err):
                # httpx.HTTPStatusError has a .response attribute
                if hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
                    if exc.response.status_code == 401 and not is_401:
                        is_401 = True
                        www_auth_401 = exc.response.headers.get("www-authenticate")
                    elif exc.response.status_code == 403 and not is_403:
                        is_403 = True
                        www_auth_403 = exc.response.headers.get("www-authenticate")
                elif isinstance(exc, (_httpx.TransportError, TimeoutError, ConnectionError)):
                    transport_failed = True
                elif isinstance(exc, RequestValidationError):
                    local_rejection = True
                else:
                    # Fallback: string matching, only for exceptions that carry
                    # no HTTP status and are not transport failures. A status
                    # error's message includes the request URL, so a port such
                    # as :8401 would otherwise read as a 401.
                    exc_str = str(exc)
                    if "401" in exc_str or "Unauthorized" in exc_str:
                        is_401 = True
            # A 401 takes precedence over a 403.
            www_auth_header = www_auth_401 if is_401 else www_auth_403

            # The MCP SDK can surface a transport 401 as a bare
            # MCPError("Server returned an error response") with no status
            # attached. When the request carried no credentials, ask the server
            # directly whether it requires auth before reporting a generic error.
            # A timeout or connection failure is reported as it is: the server
            # did not answer, so a probe would only add a second wait. A
            # RequestValidationError is murl's own rejection of the request.
            request_had_auth = any(k.lower() == 'authorization' for k in headers)
            if (not no_auth and not is_401 and not is_403 and not request_had_auth
                    and not transport_failed and not local_rejection):
                probed = _probe_www_authenticate(base_url)
                if probed is not None:
                    is_401 = True
                    www_auth_header = probed

            # MCP 2025-11-25 §Authorization Flow Steps: on 401, discover
            # metadata via WWW-Authenticate, then run full OAuth flow.
            if not no_auth and is_401:
                if verbose:
                    click.echo("Received 401 — initiating OAuth flow...", err=True)
                    if www_auth_header:
                        click.echo(f"WWW-Authenticate: {www_auth_header}", err=True)
                # A re-auth may bind a different identity; any cached session is
                # likely invalid for it, so discard it and negotiate fresh.
                if session_reuse:
                    session_store.clear_session(base_url)
                creds = authorize(base_url, www_authenticate=www_auth_header, **auth_kwargs)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
                result = asyncio.run(make_mcp_request(
                    base_url, method, params, headers, verbose,
                    raw=raw, max_pages=max_pages, session_reuse=session_reuse,
                ))
            # MCP 2025-11-25 §Scope Challenge Handling: on 403 with explicit
            # insufficient_scope error, parse required scopes and re-authorize.
            elif not no_auth and is_403:
                www_params = parse_www_authenticate(www_auth_header) if www_auth_header else {}
                if www_params.get("error") != "insufficient_scope" or not www_params.get("scope"):
                    raise
                if verbose:
                    click.echo("Received 403 insufficient_scope — re-authorizing...", err=True)
                    click.echo(f"WWW-Authenticate: {www_auth_header}", err=True)
                # Merge scope override into auth_kwargs to avoid passing scope twice.
                reauth_kwargs = {**auth_kwargs, "scope": www_params["scope"]}
                # Like the 401 path: a step-up re-auth yields a new token, so
                # discard any cached session and negotiate fresh on retry.
                if session_reuse:
                    session_store.clear_session(base_url)
                creds = authorize(base_url, www_authenticate=www_auth_header, **reauth_kwargs)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
                result = asyncio.run(make_mcp_request(
                    base_url, method, params, headers, verbose,
                    raw=raw, max_pages=max_pages, session_reuse=session_reuse,
                ))
            else:
                raise

        # --- Post-process: detect tool-level auth failure ---
        # tools/call responses are unwrapped from their {type:"text"} envelope,
        # and any nextCursor is followed transparently, inside make_mcp_request
        # — on a single session (see _follow_tool_pagination). isError and URL
        # elicitation arrive as ToolOutcome exceptions (handled below). Here we
        # only flag an auth failure a server returned as a successful result.
        # --raw and non-tools/call responses skip this.
        if not raw and method == 'tools/call':
            failure = detect_tool_auth_failure(result)
            if failure:
                raise ToolAuthError(failure, content=result)

        _emit_result(result, verbose, output_format)

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
        # murl's own rejection raised inside the session's task group reads the
        # same as when it is raised directly.
        rejection = next(
            (e for e in _flatten_exceptions(eg) if isinstance(e, RequestValidationError)), None
        )
        if rejection is not None:
            output_error(
                error_type="INVALID_ARGUMENT",
                message=str(rejection),
                exit_code=ErrorCode.INVALID_ARGUMENT
            )
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
    except OAuthError as e:
        # The OAuth flow failed (redirect-uri mismatch, port conflict, expired
        # token with no refresh, discovery failure, etc.). Surface a structured
        # AUTH_FAILED rather than the generic ERROR fall-through, and prefer the
        # failure-specific suggestion the raise site attached; otherwise fall
        # back to the most common remedy — re-authenticating.
        output_error(
            error_type="AUTH_FAILED",
            message=str(e),
            exit_code=ErrorCode.GENERAL_ERROR,
            suggestion=e.suggestion or "Re-authenticate with `murl --login <url>`.",
        )
    except ToolOutcome as e:
        _report_tool_outcome(e, verbose, output_format)
    except Exception as e:
        error_msg = str(e)
        # Classify by exception TYPE, not by substring-matching the message —
        # a tool error whose text happens to contain "ValidationError" or
        # "Timeout" must not be misclassified. Exception-name matching keeps
        # this robust without importing pydantic just for an isinstance check.
        exc_type = type(e).__name__

        if exc_type == "ValidationError":
            error_obj = {
                "error": "VALIDATION_ERROR",
                "message": f"Invalid response from server: {error_msg}",
                "code": ErrorCode.MCP_SERVER_ERROR
            }
            click.echo(json.dumps(error_obj), err=True)
            sys.exit(ErrorCode.GENERAL_ERROR)
        elif isinstance(e, _httpx.ConnectError):
            output_error(
                error_type="CONNECTION_ERROR",
                message="Failed to connect",
                exit_code=ErrorCode.GENERAL_ERROR
            )
        elif isinstance(e, _httpx.TimeoutException):
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
