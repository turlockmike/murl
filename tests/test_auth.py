"""Tests for the OAuth 2.0 auth module."""

import json
import time
import threading
import urllib.parse
from unittest.mock import patch, MagicMock

import pytest
import httpx

from murl.auth import (
    _auth_base_url,
    _generate_pkce,
    discover_metadata,
    register_client,
    authorize,
    refresh_token,
    OAuthError,
)
from murl.token_store import (
    get_credentials,
    save_credentials,
    clear_credentials,
    is_expired,
    CREDENTIALS_DIR,
)


# ---------------------------------------------------------------------------
# token_store tests
# ---------------------------------------------------------------------------

class TestTokenStore:
    """Tests for credential persistence."""

    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        creds = {"access_token": "tok123", "expires_at": time.time() + 3600}

        assert get_credentials(url) is None
        save_credentials(url, creds)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "tok123"
        assert loaded["server_url"] == url

    def test_clear(self, tmp_path, monkeypatch):
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "tok"})
        clear_credentials(url)
        assert get_credentials(url) is None

    def test_clear_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        clear_credentials("https://nope.example.com")  # should not raise

    def test_is_expired_true(self):
        assert is_expired({"expires_at": time.time() - 10})

    def test_is_expired_false(self):
        assert not is_expired({"expires_at": time.time() + 3600})

    def test_is_expired_within_buffer(self):
        # Expires in 30s — within the 60s buffer
        assert is_expired({"expires_at": time.time() + 30})

    def test_is_expired_missing(self):
        assert is_expired({})


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

class TestAuthHelpers:

    def test_auth_base_url(self):
        assert _auth_base_url("https://foo.com/mcp/default") == "https://foo.com"
        assert _auth_base_url("http://localhost:3000/mcp") == "http://localhost:3000"

    def test_pkce_verifier_and_challenge_differ(self):
        v, c = _generate_pkce()
        assert v != c
        assert len(v) > 40
        assert len(c) > 20
        # Challenge must be base64url without padding
        assert "=" not in c
        assert "+" not in c
        assert "/" not in c


# ---------------------------------------------------------------------------
# discover_metadata
# ---------------------------------------------------------------------------

class TestDiscoverMetadata:

    def test_success(self):
        meta = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
        }

        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert result == meta
            mock_get.assert_called_once()

    def test_fallback_on_404(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert "/authorize" in result["authorization_endpoint"]
            assert "/token" in result["token_endpoint"]
            assert "/register" in result["registration_endpoint"]

    def test_fallback_on_network_error(self):
        with patch("murl.auth.httpx.get", side_effect=httpx.ConnectError("fail")):
            result = discover_metadata("https://example.com/mcp")
            assert "authorization_endpoint" in result


# ---------------------------------------------------------------------------
# register_client
# ---------------------------------------------------------------------------

class TestRegisterClient:

    def test_success(self):
        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {
                "client_id": "cid_123",
                "client_secret": "csec_456",
            }
            mock_post.return_value = mock_resp

            result = register_client(
                "https://auth.example.com/register",
                "http://127.0.0.1:9999/callback",
            )
            assert result["client_id"] == "cid_123"

    def test_failure(self):
        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = "bad request"
            mock_post.return_value = mock_resp

            with pytest.raises(OAuthError, match="registration failed"):
                register_client(
                    "https://auth.example.com/register",
                    "http://127.0.0.1:9999/callback",
                )


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------

class TestRefreshToken:

    def test_success(self):
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "refresh_token": "new_rt",
                "expires_in": 7200,
            }
            mock_post.return_value = mock_resp

            updated = refresh_token(creds)
            assert updated["access_token"] == "new_at"
            assert updated["refresh_token"] == "new_rt"
            assert updated["expires_at"] > time.time()

    def test_no_refresh_token(self):
        with pytest.raises(OAuthError, match="No refresh token"):
            refresh_token({"client_id": "cid", "token_endpoint": "https://x"})

    def test_failure(self):
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt",
            "token_endpoint": "https://auth.example.com/token",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = "invalid_grant"
            mock_post.return_value = mock_resp

            with pytest.raises(OAuthError, match="refresh failed"):
                refresh_token(creds)


# ---------------------------------------------------------------------------
# Full authorize flow (mocked)
# ---------------------------------------------------------------------------

class TestAuthorize:

    def _mock_full_flow(self, mock_httpx_get, mock_httpx_post, mock_webbrowser, mock_server):
        """Set up mocks for a successful full OAuth flow."""
        # Metadata discovery
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
        }
        mock_httpx_get.return_value = meta_resp

        # Registration + token exchange (two POST calls)
        reg_resp = MagicMock()
        reg_resp.status_code = 201
        reg_resp.json.return_value = {"client_id": "cid_test"}

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_final",
            "refresh_token": "rt_final",
            "expires_in": 3600,
        }
        mock_httpx_post.side_effect = [reg_resp, token_resp]

        # Browser open — no-op
        mock_webbrowser.return_value = True

        # Callback server returns a code
        mock_server.return_value = "test_auth_code"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_full_flow(self, mock_get, mock_post, mock_browser, mock_server):
        self._mock_full_flow(mock_get, mock_post, mock_browser, mock_server)

        creds = authorize("https://example.com/mcp")

        assert creds["access_token"] == "at_final"
        assert creds["refresh_token"] == "rt_final"
        assert creds["client_id"] == "cid_test"
        assert creds["expires_at"] > time.time()
        assert creds["server_url"] == "https://example.com/mcp"

        # Verify browser was opened with correct params
        mock_browser.assert_called_once()
        auth_url = mock_browser.call_args[0][0]
        parsed = urllib.parse.urlparse(auth_url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params["client_id"] == ["cid_test"]
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert "state" in params
        assert "code_challenge" in params

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_no_registration_endpoint(self, mock_get, mock_post, mock_browser, mock_server):
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            # No registration_endpoint
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="registration endpoint"):
            authorize("https://example.com/mcp")
