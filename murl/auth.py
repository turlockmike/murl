"""OAuth 2.0 Dynamic Client Registration (RFC 7591) with PKCE."""

import base64
import hashlib
import html
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import httpx


CALLBACK_TIMEOUT = 60  # seconds to wait for browser callback


class OAuthError(Exception):
    """Raised when an OAuth operation fails."""


def _auth_base_url(server_url: str) -> str:
    """Extract scheme + host from server URL (strip path per MCP spec)."""
    parsed = urllib.parse.urlparse(server_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def discover_metadata(server_url: str) -> dict:
    """Fetch OAuth 2.0 Authorization Server Metadata.

    Tries /.well-known/oauth-authorization-server first.
    Falls back to sensible defaults if 404.
    """
    base = _auth_base_url(server_url)
    url = f"{base}/.well-known/oauth-authorization-server"

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=10)
    except httpx.HTTPError:
        # Network error — fall back to defaults
        return {
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
        }

    if resp.status_code == 200:
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise OAuthError("Invalid JSON in OAuth metadata response") from exc

    if resp.status_code == 404:
        return {
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
        }

    raise OAuthError(
        f"Failed to fetch OAuth metadata ({resp.status_code}): {resp.text}"
    )


def register_client(registration_endpoint: str, redirect_uri: str) -> dict:
    """Dynamic Client Registration (RFC 7591).

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
    """Tiny HTTP handler that captures the OAuth callback."""

    # Shared across instances via class attrs set before serve_forever()
    auth_code: Optional[str] = None
    auth_error: Optional[str] = None
    expected_state: Optional[str] = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        # Validate state
        state = params.get("state", [None])[0]
        if state != self.expected_state:
            _CallbackHandler.auth_error = "State mismatch"
            self._respond("Authorization failed: state mismatch.")
            return

        error = params.get("error", [None])[0]
        if error:
            desc = params.get("error_description", [error])[0]
            _CallbackHandler.auth_error = desc
            self._respond(f"Authorization failed: {desc}")
            return

        code = params.get("code", [None])[0]
        if not code:
            _CallbackHandler.auth_error = "No authorization code received"
            self._respond("Authorization failed: no code received.")
            return

        _CallbackHandler.auth_code = code
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
    _CallbackHandler.auth_code = None
    _CallbackHandler.auth_error = None
    _CallbackHandler.expected_state = state

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout

    # Handle a single request (the callback)
    server.handle_request()
    server.server_close()

    if _CallbackHandler.auth_error:
        raise OAuthError(_CallbackHandler.auth_error)
    if not _CallbackHandler.auth_code:
        raise OAuthError("Timed out waiting for authorization callback")
    return _CallbackHandler.auth_code


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

def authorize(server_url: str) -> dict:
    """Run the full OAuth flow and return credential dict.

    Steps: metadata discovery -> client registration -> PKCE browser auth -> token exchange.

    The returned dict contains: client_id, client_secret (maybe None),
    access_token, refresh_token, expires_at, token_endpoint,
    registration_endpoint, server_url.
    """
    import click

    # 1. Metadata
    click.echo("Discovering OAuth metadata...", err=True)
    meta = discover_metadata(server_url)
    auth_endpoint = meta["authorization_endpoint"]
    token_endpoint = meta["token_endpoint"]
    reg_endpoint = meta.get("registration_endpoint")

    if not reg_endpoint:
        raise OAuthError(
            "Server does not advertise a registration endpoint. "
            "Manual client registration may be required."
        )

    # 2. Pick a random port for the callback
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    redirect_uri = f"http://127.0.0.1:{port}/callback"

    # 3. Dynamic client registration
    click.echo("Registering client...", err=True)
    reg = register_client(reg_endpoint, redirect_uri)
    client_id = reg["client_id"]
    client_secret = reg.get("client_secret")

    # 4. PKCE + authorization URL
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"

    click.echo("Opening browser for authorization...", err=True)
    webbrowser.open(auth_url)

    # 5. Wait for callback in a background thread so we can show a message
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

    # 6. Token exchange
    click.echo("Exchanging authorization code for token...", err=True)
    token_data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
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
    }
    click.echo("Authorization successful!", err=True)
    return creds


def refresh_token(creds: dict) -> dict:
    """Use a refresh token to get a new access token.

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
