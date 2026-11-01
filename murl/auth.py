"""OAuth 2.0 authorization for MCP (2025-11-25 spec).

Implements:
- Protected Resource Metadata discovery (RFC 9728)
- Authorization Server Metadata with OIDC fallback (RFC 8414)
- Dynamic Client Registration (RFC 7591)
- PKCE with S256 (OAuth 2.1)
- Resource Indicators (RFC 8707)
"""

import base64
import hashlib
import html
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, List, Optional, Tuple

import httpx


CALLBACK_TIMEOUT = 60  # seconds to wait for browser callback

# The de-facto standard port for OAuth callback redirect URIs on localhost.
# Pre-registered SPA clients (Okta, Cognito) commonly register
# "http://localhost:8080/callback" and do exact-string matching on redirect_uri,
# so an ephemeral port would be rejected. Used as the default when neither
# --callback-port nor .mcp.json callbackPort is supplied.
STANDARD_OAUTH_CALLBACK_PORT = 8080


class OAuthError(Exception):
    """Raised when an OAuth operation fails.

    Carries an optional ``suggestion`` — a short, failure-specific remediation
    hint surfaced to the user. Each raise site can set its own (e.g. a port
    conflict suggests ``--callback-port``); when omitted, the CLI falls back to
    a generic ``--login`` hint.
    """

    def __init__(self, message: str, suggestion: Optional[str] = None) -> None:
        super().__init__(message)
        self.suggestion = suggestion


# ---------------------------------------------------------------------------
# WWW-Authenticate header parsing
# ---------------------------------------------------------------------------

def parse_www_authenticate(header_value: str) -> dict:
    """Parse a WWW-Authenticate Bearer header into a dict of parameters.

    Per MCP 2025-11-25 §Protected Resource Metadata Discovery Requirements,
    clients MUST be able to parse WWW-Authenticate headers and respond
    appropriately to HTTP 401/403 responses.

    Handles: Bearer resource_metadata="...", scope="...", error="..."
    Returns dict with keys like resource_metadata, scope, error, error_description.
    """
    result = {}
    # Strip the "Bearer" scheme prefix if present
    value = header_value.strip()
    if value.lower().startswith("bearer"):
        value = value[len("bearer"):].strip()
        # Handle case where there's nothing after Bearer (just "Bearer")
        if not value:
            return result

    # Parse key="value" pairs (values may or may not be quoted)
    for match in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', value):
        result[match.group(1)] = match.group(2)
    # Also handle unquoted values
    for match in re.finditer(r'(\w+)\s*=\s*([^",\s]+)', value):
        key = match.group(1)
        if key not in result:  # quoted takes precedence
            result[key] = match.group(2)

    return result


# ---------------------------------------------------------------------------
# Discovery: Protected Resource Metadata (RFC 9728)
# ---------------------------------------------------------------------------

def discover_resource_metadata(server_url: str, resource_metadata_url: Optional[str] = None) -> dict:
    """Fetch Protected Resource Metadata per RFC 9728.

    Per MCP 2025-11-25 §Authorization Server Location, clients MUST use
    Protected Resource Metadata for authorization server discovery.

    Per §Protected Resource Metadata Discovery Requirements, clients MUST:
      - Use the resource_metadata URL from WWW-Authenticate when present.
      - Otherwise fall back to well-known URIs in this order:
        1. /.well-known/oauth-protected-resource/<path>  (path-aware)
        2. /.well-known/oauth-protected-resource          (root)

    Returns the metadata dict, which MUST contain 'authorization_servers'.
    Raises OAuthError if discovery fails.
    """
    return _discover_resource_metadata(server_url, resource_metadata_url)[0]


def _response_hops(requested_url: str, resp: Any) -> List[str]:
    """Every URL a response passed through: the request, each redirect, the final URL."""
    hops = [requested_url]
    history = resp.history if isinstance(resp.history, list) else []
    for prior in history:
        if isinstance(prior.url, (str, httpx.URL)):
            hops.append(str(prior.url))
    if isinstance(resp.url, (str, httpx.URL)):
        hops.append(str(resp.url))
    return hops


def _discover_resource_metadata(server_url: str,
                                resource_metadata_url: Optional[str] = None
                                ) -> Tuple[dict, List[str]]:
    """``discover_resource_metadata`` plus the URLs the document was served through.

    The second element lists the requested URL, every redirect hop and the
    final URL, for ``_validate_resource_metadata`` to check where the
    document came from.
    """
    urls_to_try = []

    if resource_metadata_url:
        urls_to_try.append(resource_metadata_url)
    else:
        parsed = urllib.parse.urlparse(server_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")

        if path:
            urls_to_try.append(f"{base}/.well-known/oauth-protected-resource{path}")
        urls_to_try.append(f"{base}/.well-known/oauth-protected-resource")

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            try:
                meta = resp.json()
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and "authorization_servers" in meta:
                return meta, _response_hops(url, resp)
            # Valid JSON but missing required field — keep trying
            continue

    raise OAuthError(
        "Could not discover Protected Resource Metadata (RFC 9728). "
        "The server may not support OAuth, or the well-known endpoint is unreachable."
    )


# ---------------------------------------------------------------------------
# Discovery: Authorization Server Metadata (RFC 8414 + OIDC)
# ---------------------------------------------------------------------------

def discover_auth_server_metadata(issuer_url: str) -> dict:
    """Fetch Authorization Server Metadata with OIDC Discovery fallback.

    Per MCP 2025-11-25 §Authorization Server Metadata Discovery, clients
    MUST attempt multiple well-known endpoints.  Priority order follows
    RFC 8414 §3.1 and §5 (OIDC interop).

    For issuer with path (e.g. https://auth.example.com/tenant1), MUST try:
      1. /.well-known/oauth-authorization-server/tenant1  (RFC 8414)
      2. /.well-known/openid-configuration/tenant1        (OIDC path insertion)
      3. /tenant1/.well-known/openid-configuration        (OIDC path append)
      4. /.well-known/oauth-authorization-server          (host-level fallback)
      5. /.well-known/openid-configuration                (host-level OIDC fallback)

    For issuer without path (e.g. https://auth.example.com), MUST try:
      1. /.well-known/oauth-authorization-server           (RFC 8414)
      2. /.well-known/openid-configuration                 (OIDC)

    Host-level fallback covers servers (e.g. mcp.atlassian.com) that publish
    a single metadata document at the host root and route 401/404 for
    path-prefixed well-known URLs.
    """
    parsed = urllib.parse.urlparse(issuer_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    urls_to_try = []
    if path:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server{path}")
        urls_to_try.append(f"{base}/.well-known/openid-configuration{path}")
        urls_to_try.append(f"{base}{path}/.well-known/openid-configuration")
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server")
        urls_to_try.append(f"{base}/.well-known/openid-configuration")
    else:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server")
        urls_to_try.append(f"{base}/.well-known/openid-configuration")

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            try:
                meta = resp.json()
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and "token_endpoint" in meta:
                # Tag the source so callers can distinguish RFC 8414 from OIDC.
                # OIDC Provider Metadata does not define code_challenge_methods_supported,
                # so its absence from an OIDC endpoint is expected (see PKCE check).
                meta["_discovery_source"] = (
                    "oidc" if "openid-configuration" in url else "rfc8414"
                )
                return meta

    raise OAuthError(
        f"Could not discover Authorization Server Metadata for {issuer_url}. "
        "Tried OAuth 2.0 (RFC 8414) and OpenID Connect discovery endpoints."
    )


# ---------------------------------------------------------------------------
# Legacy discovery (kept for backwards compat with servers that don't
# implement RFC 9728 yet)
# ---------------------------------------------------------------------------

def _auth_base_url(server_url: str) -> str:
    """Extract scheme + host from server URL."""
    parsed = urllib.parse.urlparse(server_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def discover_metadata(server_url: str) -> dict:
    """Legacy: fetch OAuth 2.0 Authorization Server Metadata directly.

    Tries /.well-known/oauth-authorization-server first, with OIDC fallback.
    Falls back to sensible defaults if both 404.
    """
    parsed = urllib.parse.urlparse(server_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    # Try RFC 8414 then OIDC, path-aware, with host-level fallback for servers
    # (e.g. mcp.atlassian.com) that publish metadata only at the host root.
    urls_to_try = []
    if path:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server{path}")
        urls_to_try.append(f"{base}/.well-known/openid-configuration{path}")
        urls_to_try.append(f"{base}{path}/.well-known/openid-configuration")
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server")
        urls_to_try.append(f"{base}/.well-known/openid-configuration")
    else:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server")
        urls_to_try.append(f"{base}/.well-known/openid-configuration")

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            try:
                meta = resp.json()
            except json.JSONDecodeError as exc:
                raise OAuthError("Invalid JSON in OAuth metadata response") from exc
            # Tag the source so callers (e.g. authorize()'s PKCE check) can
            # distinguish RFC 8414 from OIDC and warn-rather-than-fail when an
            # OIDC document omits code_challenge_methods_supported.
            if isinstance(meta, dict):
                meta["_discovery_source"] = (
                    "oidc" if "openid-configuration" in url else "rfc8414"
                )
            return meta

    # All attempts failed — fall back to defaults
    return {
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
    }


def register_client(registration_endpoint: str, redirect_uri: str) -> dict:
    """Dynamic Client Registration (RFC 7591).

    Per MCP 2025-11-25 §Client Registration Approaches, DCR is a MAY-level
    fallback used when the client has no pre-registered credentials and the
    auth server does not support Client ID Metadata Documents.

    Registers as a public client (token_endpoint_auth_method=none).
    Returns dict with at least ``client_id`` and optionally ``client_secret``.
    """
    payload = {
        "client_name": "murl",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    resp = httpx.post(
        registration_endpoint,
        json=payload,
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise OAuthError(
            f"Client registration failed ({resp.status_code}): {resp.text}"
        )
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(
            f"Client registration returned invalid JSON "
            f"({resp.status_code}): {resp.text}"
        ) from exc


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler that captures the OAuth callback.

    Per-request state is stored on ``self.server.callback_state`` (a dict set
    by ``_run_callback_server``) instead of class attributes, so concurrent
    ``authorize()`` calls don't race-write to shared slots.
    """

    def do_GET(self):  # noqa: N802
        ctx = self.server.callback_state
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        # Validate state
        state = params.get("state", [None])[0]
        if state != ctx["expected_state"]:
            ctx["auth_error"] = "State mismatch"
            self._respond("Authorization failed: state mismatch.")
            return

        error = params.get("error", [None])[0]
        if error:
            desc = params.get("error_description", [error])[0]
            ctx["auth_error"] = desc
            self._respond(f"Authorization failed: {desc}")
            return

        code = params.get("code", [None])[0]
        if not code:
            ctx["auth_error"] = "No authorization code received"
            self._respond("Authorization failed: no code received.")
            return

        ctx["auth_code"] = code
        self._respond("Authorization successful! You can close this tab.")

    def _respond(self, body: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        safe_body = html.escape(body)
        page = (
            "<html><body style='font-family:system-ui;text-align:center;"
            f"padding:3em'><h2>{safe_body}</h2></body></html>"
        )
        self.wfile.write(page.encode())

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


def _run_callback_server(port: int, state: str, timeout: float) -> str:
    """Start a local server, wait for the callback, return the auth code."""
    # Bind to "localhost" (not "127.0.0.1") so the listener matches the
    # redirect_uri host.  On machines where localhost resolves to ::1 first,
    # binding 127.0.0.1 would miss IPv6 callbacks from the browser.
    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.timeout = timeout
    # Per-instance state dict avoids class-level attributes that would be
    # shared (and race-written) across concurrent authorize() calls.
    server.callback_state = {
        "auth_code": None,
        "auth_error": None,
        "expected_state": state,
    }

    # Handle a single request (the callback)
    server.handle_request()
    server.server_close()

    ctx = server.callback_state
    if ctx["auth_error"]:
        raise OAuthError(ctx["auth_error"])
    if not ctx["auth_code"]:
        raise OAuthError("Timed out waiting for authorization callback")
    return ctx["auth_code"]


def _select_callback_port(callback_port: Optional[int]) -> int:
    """Resolve and validate the OAuth callback port.

    Returns ``callback_port`` when supplied, otherwise the standard 8080. IDPs
    do exact-string matching on redirect_uri and pre-registered SPA clients
    almost always register ``http://localhost:8080/callback``, so an ephemeral
    port would be rejected. Precedence (explicit flag > .mcp.json) is enforced
    by the caller — any value arriving here as ``callback_port`` already won.

    Probes the chosen port before returning so a conflict fails fast with
    actionable guidance instead of hanging until the callback times out. The
    probe socket is closed immediately and the real HTTPServer binds later; the
    TOCTOU window is acceptable for a local-dev CLI.

    The probe binds with the *same* address family and host the real callback
    server uses (``HTTPServer((\"localhost\", port))`` — ``HTTPServer`` pins
    ``address_family`` to AF_INET, so both resolve ``localhost`` to 127.0.0.1).
    Using ``HTTPServer.address_family`` keeps the probe in lockstep if the
    server's family is ever changed (e.g. an IPv6 subclass), so the probe can't
    silently diverge from what actually binds below.

    Raises OAuthError (with a ``--callback-port`` suggestion) if the port is in use.
    """
    port = callback_port if callback_port else STANDARD_OAUTH_CALLBACK_PORT
    try:
        with socket.socket(HTTPServer.address_family, socket.SOCK_STREAM) as probe:
            probe.bind(("localhost", port))
    except OSError as exc:
        raise OAuthError(
            f"Callback port {port} is already in use.",
            suggestion=(
                "Pass --callback-port <free-port> and register "
                "http://localhost:<free-port>/callback with your IdP."
            ),
        ) from exc
    return port


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_pkce() -> tuple:
    """Return (code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _canonical_resource_uri(server_url: str) -> str:
    """Build the canonical resource URI for RFC 8707 resource parameter.

    Per MCP 2025-11-25 §Resource Parameter Implementation:
      - MUST use the canonical URI of the MCP server (RFC 8707 §2).
      - MUST NOT include fragments.
      - SHOULD use the form without trailing slash unless semantically significant.
      - SHOULD provide the most specific URI possible.
    """
    parsed = urllib.parse.urlparse(server_url)
    # Reconstruct without fragment or query; keep path as-is (no synthetic "/")
    # so that "https://example.com" stays "https://example.com" per spec SHOULD.
    return urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, parsed.path, "", "", ""
    ))


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _resource_key(url: str) -> Optional[Tuple[str, str, Optional[int], str, str]]:
    """Comparison key for a resource identifier: scheme, host, port, path, query.

    Scheme and host compare case-insensitively (RFC 3986 §6.2.2.1), a default
    port equals an omitted one, and one trailing "/" is ignored. The fragment
    is dropped. The path is compared as written; percent-encoding is not
    normalized. Returns None for a URL that does not parse (a malformed IPv6
    host, a port out of range), which never matches.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    return (scheme, (parsed.hostname or "").lower(), port, path, parsed.query)


def _resource_matches(resource: Any, server_url: str) -> bool:
    """True when the metadata's ``resource`` identifies ``server_url`` (RFC 9728 §3.3).

    A query in ``resource`` must equal the requested query. A ``resource``
    without a query matches any query on the requested URL, since murl's
    resource identifier (``_canonical_resource_uri``) drops it.
    """
    if not isinstance(resource, str):
        return False
    want = _resource_key(server_url)
    got = _resource_key(resource)
    if want is None or got is None or got[:4] != want[:4]:
        return False
    return not got[4] or got[4] == want[4]


def _hostname(url: str) -> str:
    """Lowercased hostname, or "" for a URL that does not parse."""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_tls_or_loopback(url: str, server_url: str) -> bool:
    """True for an https URL, or an http URL on a loopback host when the MCP server is on one too."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme == "https":
        return True
    return (
        scheme == "http"
        and _hostname(url) in _LOOPBACK_HOSTS
        and _hostname(server_url) in _LOOPBACK_HOSTS
    )


def _validate_resource_metadata(resource_meta: dict, prm_hops: List[str],
                                auth_server_url: str, server_url: str) -> None:
    """Check that Protected Resource Metadata may name ``auth_server_url`` for ``server_url``.

    ``prm_hops`` lists every URL the metadata was served through: the
    requested URL, each redirect and the final URL.

    - The authorization server URL must be https. http is accepted only on a
      loopback host, and only when the MCP server is on a loopback host.
    - An authorization server on another host is accepted only when the
      metadata's ``resource`` identifies the resource murl requested (RFC 9728
      §3.3) and every hop is on the MCP server's hostname over https (loopback
      http for a loopback MCP server). ``resource`` is self-asserted, so a
      match alone binds nothing: any server can publish metadata claiming
      another server's URL, and ``WWW-Authenticate: resource_metadata`` can
      point anywhere. Atlassian, Okta-fronted servers and Auth0 deployments
      serve their metadata this way.
    - Otherwise the authorization server must share the MCP server's
      hostname. This covers metadata without ``resource`` (RFC 9728 §2 makes
      it REQUIRED), a ``resource`` that names another URL (a server publishing
      only the root document with its bare origin, e.g. Notion's
      ``"resource": "https://mcp.notion.com"``), and metadata served from
      another host or in plaintext. ``--client-id`` callers skip these checks
      in ``authorize``.

    Raises OAuthError when a check fails.
    """
    auth_host = _hostname(auth_server_url) if isinstance(auth_server_url, str) else ""
    if not auth_host:
        raise OAuthError(
            f"Invalid authorization server URL: {auth_server_url!r}"
        )
    if not _is_tls_or_loopback(auth_server_url, server_url):
        raise OAuthError(
            f"Authorization server URL '{auth_server_url}' must use https."
        )

    server_host = _hostname(server_url)
    if "resource" not in resource_meta:
        reason = "has no 'resource' field to bind it to this server"
    elif not _resource_matches(resource_meta["resource"], server_url):
        reason = (
            f"names resource {resource_meta['resource']!r}, not the requested "
            f"{_canonical_resource_uri(server_url)!r} (RFC 9728 §3.3)"
        )
    elif prm_hops and all(
        server_host and _hostname(hop) == server_host
        and _is_tls_or_loopback(hop, server_url)
        for hop in prm_hops
    ):
        return
    else:
        reason = "was not served by the MCP server's host over https"

    # Compare hostnames (case-insensitive per RFC 4343).
    if server_host != auth_host:
        raise OAuthError(
            f"Authorization server hostname '{auth_host}' does not match "
            f"MCP server hostname '{server_host}', and the Protected Resource "
            f"Metadata {reason}. "
            f"This could indicate a malicious server redirect. "
            f"If this is expected, supply pre-registered credentials with --client-id "
            f"to bypass this check."
        )


def authorize(server_url: str, www_authenticate: Optional[str] = None,
              scope: Optional[str] = None, client_id: Optional[str] = None,
              client_secret: Optional[str] = None,
              callback_port: Optional[int] = None) -> dict:
    """Run the full OAuth flow and return credential dict.

    Follows MCP 2025-11-25 authorization spec:
      1. Protected Resource Metadata discovery (RFC 9728)
      2. Authorization Server Metadata discovery (RFC 8414 + OIDC)
      3. Client registration (pre-configured or Dynamic Client Registration)
      4. PKCE browser auth with resource parameter (RFC 8707)
      5. Token exchange with resource parameter

    Args:
        server_url: The MCP server base URL.
        www_authenticate: Optional WWW-Authenticate header value from a 401 response.
        scope: Optional scope string to request (from WWW-Authenticate or resource metadata).
        client_id: Optional pre-registered OAuth client ID. Skips Dynamic Client Registration.
        client_secret: Optional pre-registered OAuth client secret.
        callback_port: Optional fixed port for the OAuth callback server.
            Required when using pre-registered credentials with a fixed redirect URI.

    Returns dict with: client_id, client_secret, access_token, refresh_token,
    expires_at, token_endpoint, registration_endpoint, server_url, resource_uri.
    """
    import click

    resource_uri = _canonical_resource_uri(server_url)

    # --- Step 1: Discovery ---
    # Try the full RFC 9728 discovery chain first. If that fails (server doesn't
    # implement Protected Resource Metadata), fall back to legacy direct discovery.
    click.echo("Discovering OAuth metadata...", err=True)

    www_auth_params = parse_www_authenticate(www_authenticate) if www_authenticate else {}
    resource_metadata_url = www_auth_params.get("resource_metadata")

    # MCP 2025-11-25 §Scope Selection Strategy — priority order:
    #   1. Explicit scope param (from caller, e.g. 403 step-up)
    #   2. scope from WWW-Authenticate header
    #   3. scopes_supported from Protected Resource Metadata
    #   4. Omit scope entirely if none available
    if not scope and www_auth_params.get("scope"):
        scope = www_auth_params["scope"]

    # Two-phase discovery: first try RFC 9728 resource metadata, then fetch
    # auth server metadata from the advertised issuer.  Only fall back to
    # legacy discovery when resource metadata itself is unavailable — if the
    # issuer is found but its metadata fetch fails, that error must propagate.
    try:
        resource_meta, prm_hops = _discover_resource_metadata(server_url, resource_metadata_url)
    except OAuthError:
        # Resource metadata unavailable — fall back to legacy direct discovery
        # for servers not yet on MCP 2025-11-25.
        auth_server_meta = discover_metadata(server_url)
    else:
        # Extract scope from resource metadata if not already set
        if not scope and resource_meta.get("scopes_supported"):
            scope = " ".join(resource_meta["scopes_supported"])

        # Get the first authorization server
        auth_servers = resource_meta.get("authorization_servers", [])
        if not auth_servers:
            raise OAuthError("Protected Resource Metadata has empty authorization_servers")

        issuer_url = auth_servers[0]

        # Validate the metadata before trusting the authorization server it
        # names, so a hostile endpoint cannot steer the OAuth flow to an
        # attacker-controlled AS. Skip when the caller supplies pre-registered
        # credentials (--client-id), but warn loudly so the user can verify the
        # issuer is actually their provider — this check is the only guard
        # against that redirect.
        if not client_id:
            _validate_resource_metadata(resource_meta, prm_hops, issuer_url, server_url)
        else:
            click.echo(
                f"Warning: skipping resource metadata checks for authorization server "
                f"{issuer_url!r} (advertised by {server_url!r}). "
                f"Pre-registered --client-id supplied; verify this is your expected provider.",
                err=True,
            )

        # Fetch auth server metadata (RFC 8414 + OIDC fallback).
        # Errors here propagate — the issuer was explicitly advertised.
        auth_server_meta = discover_auth_server_metadata(issuer_url)

    meta = auth_server_meta
    auth_endpoint = meta["authorization_endpoint"]
    token_endpoint = meta["token_endpoint"]
    reg_endpoint = meta.get("registration_endpoint")

    # MCP 2025-11-25 §Authorization Code Protection: clients MUST verify PKCE
    # support via code_challenge_methods_supported before proceeding, and MUST
    # use S256 when technically capable (OAuth 2.1 §4.1.1).
    #
    # However, OIDC Provider Metadata (OpenID Connect Discovery 1.0) does NOT
    # define code_challenge_methods_supported — it's an OAuth 2.0 AS Metadata
    # field.  Major IDPs like Okta serve OIDC metadata without this field yet
    # fully support S256 PKCE.  The MCP spec acknowledges this:
    #   "this field is commonly included by OpenID providers"
    # When metadata comes from an OIDC endpoint, we warn but proceed with S256
    # rather than blocking all Okta/OIDC-based servers.
    challenge_methods = meta.get("code_challenge_methods_supported")
    discovery_source = meta.get("_discovery_source", "unknown")
    if challenge_methods is None:
        if discovery_source == "oidc":
            click.echo(
                "Warning: OIDC metadata does not include "
                "code_challenge_methods_supported; assuming S256 is supported.",
                err=True,
            )
        else:
            raise OAuthError(
                "Authorization server does not advertise PKCE support "
                "(code_challenge_methods_supported). Cannot proceed securely."
            )
    elif "S256" not in challenge_methods:
        raise OAuthError(
            "Authorization server does not support S256 PKCE code challenge method"
        )

    # --- Step 2: Callback port ---
    port = _select_callback_port(callback_port)

    # Use "localhost" rather than "127.0.0.1" — most IDPs (Okta, Cognito, etc.)
    # register redirect URIs with "localhost".  Both resolve to loopback, but
    # IDPs do exact string matching on the redirect_uri parameter.
    redirect_uri = f"http://localhost:{port}/callback"

    # --- Step 3: Client registration ---
    # MCP 2025-11-25 §Client Registration Approaches priority:
    #   1. Pre-registered credentials (--client-id)
    #   2. Client ID Metadata Documents (not implemented — SHOULD level)
    #   3. Dynamic Client Registration (fallback)
    #   4. Prompt user (we raise with guidance to use --client-id)
    if client_id:
        click.echo("Using pre-configured client credentials...", err=True)
    else:
        if not reg_endpoint:
            raise OAuthError(
                "Server does not advertise a registration endpoint. "
                "Use --client-id to provide pre-registered credentials."
            )
        click.echo("Registering client...", err=True)
        reg = register_client(reg_endpoint, redirect_uri)
        client_id = reg["client_id"]
        client_secret = reg.get("client_secret")

    # --- Step 4: PKCE + authorization URL ---
    # MCP 2025-11-25 §Resource Parameter Implementation: resource MUST be
    # included in both authorization requests and token requests (RFC 8707).
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": resource_uri,
    }
    if scope:
        auth_params["scope"] = scope
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"

    click.echo("Opening browser for authorization...", err=True)
    webbrowser.open(auth_url)

    # --- Step 5: Wait for callback ---
    code_result = [None]
    error_result = [None]

    def _wait():
        try:
            code_result[0] = _run_callback_server(port, state, CALLBACK_TIMEOUT)
        except OAuthError as e:
            error_result[0] = e

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    click.echo("Waiting for authorization (press Ctrl+C to cancel)...", err=True)
    t.join(timeout=CALLBACK_TIMEOUT + 5)

    if error_result[0]:
        raise error_result[0]
    if not code_result[0]:
        raise OAuthError("Timed out waiting for authorization callback")

    auth_code = code_result[0]

    # --- Step 6: Token exchange ---
    # RFC 8707: resource MUST be included in token requests (MCP 2025-11-25).
    click.echo("Exchanging authorization code for token...", err=True)
    token_data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "resource": resource_uri,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    resp = httpx.post(token_endpoint, data=token_data, timeout=10)
    if resp.status_code != 200:
        raise OAuthError(f"Token exchange failed ({resp.status_code}): {resp.text}")

    try:
        token = resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(
            f"Token exchange returned invalid JSON ({resp.status_code}): {resp.text}"
        ) from exc
    expires_in = token.get("expires_in", 3600)

    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": time.time() + expires_in,
        "token_endpoint": token_endpoint,
        "registration_endpoint": reg_endpoint,
        "server_url": server_url,
        "resource_uri": resource_uri,
    }
    click.echo("Authorization successful!", err=True)
    return creds


def refresh_token(creds: dict) -> dict:
    """Use a refresh token to get a new access token.

    Per MCP 2025-11-25 §Resource Parameter Implementation, the ``resource``
    parameter MUST be included in token requests — including refresh requests
    (RFC 8707 §2).  This ensures the refreshed token is audience-bound to the
    same MCP server.

    Returns updated credential dict.
    Raises OAuthError if refresh fails.
    """
    rt = creds.get("refresh_token")
    if not rt:
        raise OAuthError("No refresh token available")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": creds["client_id"],
    }
    if creds.get("client_secret"):
        data["client_secret"] = creds["client_secret"]
    # RFC 8707: resource MUST be included in token requests (MCP 2025-11-25).
    if creds.get("resource_uri"):
        data["resource"] = creds["resource_uri"]

    resp = httpx.post(creds["token_endpoint"], data=data, timeout=10)
    if resp.status_code != 200:
        raise OAuthError(f"Token refresh failed ({resp.status_code}): {resp.text}")

    try:
        token = resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(
            f"Token refresh returned invalid JSON: {resp.text}"
        ) from exc
    expires_in = token.get("expires_in", 3600)

    creds["access_token"] = token["access_token"]
    if "refresh_token" in token:
        creds["refresh_token"] = token["refresh_token"]
    creds["expires_at"] = time.time() + expires_in
    return creds
