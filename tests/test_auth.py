"""Tests for the OAuth 2.0 auth module."""

import socket
import time
import urllib.parse
from unittest.mock import patch, MagicMock

import pytest
import httpx

from murl.auth import (
    _auth_base_url,
    _canonical_resource_uri,
    _generate_pkce,
    _discover_resource_metadata,
    _validate_resource_metadata,
    discover_metadata,
    discover_resource_metadata,
    discover_auth_server_metadata,
    parse_www_authenticate,
    register_client,
    authorize,
    refresh_token,
    OAuthError,
    _select_callback_port,
    STANDARD_OAUTH_CALLBACK_PORT,
)
from murl.token_store import (
    get_credentials,
    save_credentials,
    clear_credentials,
    is_expired,
    _keyring_available,
    _KEYRING_SERVICE,
    _key_for_url,
)

import json


# ---------------------------------------------------------------------------
# token_store tests — helpers
# ---------------------------------------------------------------------------

def _mock_keyring_get(store, key):
    """In-memory keyring get."""
    data = store.get((_KEYRING_SERVICE, key))
    if data is None:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def _mock_keyring_set(store, key, creds):
    """In-memory keyring set."""
    store[(_KEYRING_SERVICE, key)] = json.dumps(creds)
    return True


def _mock_keyring_delete(store, key):
    """In-memory keyring delete."""
    store.pop((_KEYRING_SERVICE, key), None)


def _disable_keyring(monkeypatch):
    """Force the filesystem backend by making keyring appear unavailable."""
    monkeypatch.setattr("murl.token_store._keyring_available", lambda: False)


class TestTokenStoreFilesystem:
    """Tests for credential persistence via filesystem (no keyring)."""

    def test_roundtrip(self, tmp_path, monkeypatch):
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        creds = {"access_token": "tok123", "expires_at": time.time() + 3600}

        assert get_credentials(url) is None
        save_credentials(url, creds)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "tok123"
        assert loaded["server_url"] == url
        # Verify it's on disk
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_clear(self, tmp_path, monkeypatch):
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "tok"})
        clear_credentials(url)
        assert get_credentials(url) is None

    def test_clear_nonexistent(self, tmp_path, monkeypatch):
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        clear_credentials("https://nope.example.com")  # should not raise


class TestTokenStoreKeyring:
    """Tests for credential persistence via system keychain."""

    def _mock_keyring(self, monkeypatch):
        """Set up an in-memory keyring mock and enable it."""
        store = {}

        def get_password(service, key):
            return store.get((service, key))

        def set_password(service, key, value):
            store[(service, key)] = value

        def delete_password(service, key):
            store.pop((service, key), None)

        mock_keyring = MagicMock()
        mock_keyring.get_password = get_password
        mock_keyring.set_password = set_password
        mock_keyring.delete_password = delete_password

        monkeypatch.setattr("murl.token_store._keyring_available", lambda: True)
        import murl.token_store as ts
        monkeypatch.setattr(ts, "_keyring_get", lambda key: _mock_keyring_get(store, key))
        monkeypatch.setattr(ts, "_keyring_set", lambda key, creds: _mock_keyring_set(store, key, creds))
        monkeypatch.setattr(ts, "_keyring_delete", lambda key: _mock_keyring_delete(store, key))
        return store

    def test_roundtrip(self, tmp_path, monkeypatch):
        self._mock_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        creds = {"access_token": "tok_keyring", "expires_at": time.time() + 3600}

        assert get_credentials(url) is None
        save_credentials(url, creds)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "tok_keyring"
        assert loaded["server_url"] == url
        # No file should exist — creds are in the keychain.
        assert len(list(tmp_path.glob("*.json"))) == 0

    def test_clear(self, tmp_path, monkeypatch):
        self._mock_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "tok"})
        clear_credentials(url)
        assert get_credentials(url) is None

    def test_migration_from_file(self, tmp_path, monkeypatch):
        """Credentials saved to file are readable once keyring is enabled."""
        # First, save to filesystem.
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "old_file_tok"})
        assert len(list(tmp_path.glob("*.json"))) == 1

        # Now enable keyring — get should find the file credential.
        self._mock_keyring(monkeypatch)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "old_file_tok"

        # Re-saving moves it to keychain and cleans up the file.
        save_credentials(url, {"access_token": "new_keyring_tok"})
        loaded = get_credentials(url)
        assert loaded["access_token"] == "new_keyring_tok"
        assert len(list(tmp_path.glob("*.json"))) == 0

    def test_keyring_failure_falls_back_to_file(self, tmp_path, monkeypatch):
        """If keyring save fails, credentials go to a file instead."""
        monkeypatch.setattr("murl.token_store._keyring_available", lambda: True)
        import murl.token_store as ts
        monkeypatch.setattr(ts, "_keyring_set", lambda key, creds: False)
        monkeypatch.setattr(ts, "_keyring_get", lambda key: None)
        monkeypatch.setattr(ts, "_keyring_delete", lambda key: None)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)

        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "fallback_tok"})
        # Should have fallen back to file.
        assert len(list(tmp_path.glob("*.json"))) == 1


class TestIsExpired:
    """Tests for token expiry checks."""

    def test_expired(self):
        assert is_expired({"expires_at": time.time() - 10})

    def test_not_expired(self):
        assert not is_expired({"expires_at": time.time() + 3600})

    def test_within_buffer(self):
        # Expires in 30s — within the 60s buffer
        assert is_expired({"expires_at": time.time() + 30})

    def test_missing(self):
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
            assert result["token_endpoint"] == meta["token_endpoint"]
            assert result["_discovery_source"] == "rfc8414"
            # First URL tried should be path-aware RFC 8414
            first_url = mock_get.call_args_list[0][0][0]
            assert "/.well-known/oauth-authorization-server/mcp" in first_url

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

    def test_fallback_on_500(self):
        """All endpoints returning 500 falls back to defaults (tries multiple URLs)."""
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert "/authorize" in result["authorization_endpoint"]
            assert "/token" in result["token_endpoint"]
            # Should have tried multiple URLs (RFC 8414 + OIDC)
            assert mock_get.call_count >= 2

    def test_host_level_fallback_for_pathed_url(self):
        """Pathed server URL falls back to host-level metadata when path-aware URLs fail.

        Models mcp.atlassian.com: path-prefixed well-known URLs return 401/404,
        but the host root publishes a single OAuth metadata document.
        """
        meta = {
            "authorization_endpoint": "https://mcp.example.com/authorize",
            "token_endpoint": "https://mcp.example.com/token",
            "code_challenge_methods_supported": ["S256"],
        }
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            host_root = MagicMock()
            host_root.status_code = 200
            host_root.json.return_value = meta
            # Three path-aware attempts fail, then the host-level RFC 8414 URL succeeds.
            mock_get.side_effect = [not_found, not_found, not_found, host_root]

            result = discover_metadata("https://mcp.example.com/v1/mcp")
            assert result["token_endpoint"] == meta["token_endpoint"]
            assert result["_discovery_source"] == "rfc8414"
            assert mock_get.call_args_list[3][0][0] == "https://mcp.example.com/.well-known/oauth-authorization-server"

    def test_discovery_source_tagged_rfc8414(self):
        """RFC 8414 success tags the doc as rfc8414 so the PKCE check is strict."""
        meta = {
            "authorization_endpoint": "https://example.com/authorize",
            "token_endpoint": "https://example.com/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert result["_discovery_source"] == "rfc8414"

    def test_discovery_source_tagged_oidc(self):
        """OIDC fallback tags the doc as oidc so the PKCE check warn-and-proceeds.

        Without this tag, an OIDC document that omits code_challenge_methods_supported
        would cause the PKCE check in authorize() to hard-fail rather than warn.
        """
        meta = {
            "authorization_endpoint": "https://example.com/authorize",
            "token_endpoint": "https://example.com/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            oidc_resp = MagicMock()
            oidc_resp.status_code = 200
            oidc_resp.json.return_value = meta
            # First (RFC 8414 path-aware) fails, second (OIDC path-aware) succeeds.
            mock_get.side_effect = [not_found, oidc_resp]

            result = discover_metadata("https://example.com/mcp")
            assert result["_discovery_source"] == "oidc"
            assert "openid-configuration" in mock_get.call_args_list[1][0][0]


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
    """MCP 2025-11-25 §Resource Parameter Implementation / RFC 8707.

    refresh_token() MUST include the ``resource`` parameter in the token
    request so that the refreshed access token is audience-bound.
    """

    def test_success(self):
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            "resource_uri": "https://mcp.example.com/mcp",
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

    def test_resource_included_in_refresh_request(self):
        """RFC 8707: resource MUST be sent in token requests including refresh."""
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            "resource_uri": "https://mcp.example.com/mcp",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_resp

            refresh_token(creds)

            post_data = mock_post.call_args[1].get("data", {})
            assert post_data["resource"] == "https://mcp.example.com/mcp"

    def test_resource_omitted_when_missing_from_creds(self):
        """Legacy creds without resource_uri should not break refresh."""
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            # No resource_uri — pre-existing creds from before spec update
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_resp

            refresh_token(creds)

            post_data = mock_post.call_args[1].get("data", {})
            assert "resource" not in post_data

    def test_client_secret_included_in_refresh(self):
        """Confidential clients (pre-configured) MUST send client_secret on refresh."""
        creds = {
            "client_id": "cid",
            "client_secret": "sec123",
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            "resource_uri": "https://mcp.example.com/mcp",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_resp

            refresh_token(creds)

            post_data = mock_post.call_args[1].get("data", {})
            assert post_data["client_secret"] == "sec123"
            assert post_data["resource"] == "https://mcp.example.com/mcp"

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
# parse_www_authenticate
# ---------------------------------------------------------------------------

class TestParseWwwAuthenticate:

    def test_bearer_with_resource_metadata(self):
        header = 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", scope="files:read"'
        result = parse_www_authenticate(header)
        assert result["resource_metadata"] == "https://mcp.example.com/.well-known/oauth-protected-resource"
        assert result["scope"] == "files:read"

    def test_bearer_with_error(self):
        header = 'Bearer error="insufficient_scope", scope="files:read files:write", error_description="Need write access"'
        result = parse_www_authenticate(header)
        assert result["error"] == "insufficient_scope"
        assert result["scope"] == "files:read files:write"
        assert result["error_description"] == "Need write access"

    def test_bare_bearer(self):
        result = parse_www_authenticate("Bearer")
        assert result == {}

    def test_empty(self):
        result = parse_www_authenticate("")
        assert result == {}


# ---------------------------------------------------------------------------
# discover_resource_metadata (RFC 9728)
# ---------------------------------------------------------------------------

class TestDiscoverResourceMetadata:

    def test_hops_record_requested_url(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"authorization_servers": ["https://a.example.com"]}
            mock_get.return_value = mock_resp

            meta, hops = _discover_resource_metadata("https://mcp.example.com/mcp")
        assert hops == ["https://mcp.example.com/.well-known/oauth-protected-resource/mcp"]
        assert "_fetched_from" not in meta

    def test_hops_record_every_redirect_and_final_url(self):
        with patch("murl.auth.httpx.get") as mock_get:
            first = MagicMock()
            first.url = httpx.URL("https://mcp.example.com/.well-known/oauth-protected-resource/mcp")
            second = MagicMock()
            second.url = httpx.URL("https://evil.attacker.com/hop")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.history = [first, second]
            mock_resp.url = httpx.URL("https://mcp.example.com/prm")
            mock_resp.json.return_value = {"authorization_servers": ["https://a.example.com"]}
            mock_get.return_value = mock_resp

            _, hops = _discover_resource_metadata("https://mcp.example.com/mcp")
        assert "https://evil.attacker.com/hop" in hops
        assert hops[-1] == "https://mcp.example.com/prm"

    def test_hops_start_at_www_authenticate_url(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"authorization_servers": ["https://a.example.com"]}
            mock_get.return_value = mock_resp

            _, hops = _discover_resource_metadata(
                "https://mcp.example.com/mcp", "https://evil.attacker.com/prm",
            )
        assert hops == ["https://evil.attacker.com/prm"]

    def test_from_explicit_url(self):
        meta = {
            "resource": "https://mcp.example.com/mcp",
            "authorization_servers": ["https://auth.example.com"],
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_resource_metadata(
                "https://mcp.example.com/mcp",
                resource_metadata_url="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
            )
            assert result["authorization_servers"] == ["https://auth.example.com"]
            # Should use the explicit URL first
            mock_get.assert_called_once_with(
                "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
                follow_redirects=True, timeout=10
            )

    def test_well_known_path_aware(self):
        meta = {
            "resource": "https://mcp.example.com/mcp",
            "authorization_servers": ["https://auth.example.com"],
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_resource_metadata("https://mcp.example.com/mcp")
            assert result["authorization_servers"] == ["https://auth.example.com"]
            first_url = mock_get.call_args_list[0][0][0]
            assert first_url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"

    def test_fallback_to_root(self):
        """If path-aware URL fails, try root well-known."""
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            root_meta = MagicMock()
            root_meta.status_code = 200
            root_meta.json.return_value = {
                "authorization_servers": ["https://auth.example.com"],
            }
            mock_get.side_effect = [not_found, root_meta]

            result = discover_resource_metadata("https://mcp.example.com/mcp")
            assert result["authorization_servers"] == ["https://auth.example.com"]
            assert mock_get.call_count == 2

    def test_all_fail_raises(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            with pytest.raises(OAuthError, match="Protected Resource Metadata"):
                discover_resource_metadata("https://mcp.example.com/mcp")


# ---------------------------------------------------------------------------
# discover_auth_server_metadata (RFC 8414 + OIDC)
# ---------------------------------------------------------------------------

class TestDiscoverAuthServerMetadata:

    def test_rfc8414_success(self):
        meta = {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_auth_server_metadata("https://auth.example.com")
            assert result["token_endpoint"] == "https://auth.example.com/token"
            assert result["_discovery_source"] == "rfc8414"

    def test_oidc_fallback(self):
        """RFC 8414 fails, OIDC discovery succeeds."""
        meta = {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            oidc_resp = MagicMock()
            oidc_resp.status_code = 200
            oidc_resp.json.return_value = meta
            mock_get.side_effect = [not_found, oidc_resp]

            result = discover_auth_server_metadata("https://auth.example.com")
            assert result["token_endpoint"] == "https://auth.example.com/token"
            assert result["_discovery_source"] == "oidc"
            assert mock_get.call_count == 2

    def test_with_path_component(self):
        """Issuer URL with path uses path-insertion for both RFC 8414 and OIDC."""
        meta = {
            "issuer": "https://auth.example.com/tenant1",
            "authorization_endpoint": "https://auth.example.com/tenant1/authorize",
            "token_endpoint": "https://auth.example.com/tenant1/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_auth_server_metadata("https://auth.example.com/tenant1")
            first_url = mock_get.call_args_list[0][0][0]
            assert first_url == "https://auth.example.com/.well-known/oauth-authorization-server/tenant1"

    def test_all_fail_raises(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            with pytest.raises(OAuthError, match="Could not discover Authorization Server"):
                discover_auth_server_metadata("https://auth.example.com")

    def test_host_level_fallback_for_pathed_issuer(self):
        """Issuer with path falls back to host-level metadata when path-aware URLs fail.

        Models mcp.atlassian.com: path-prefixed well-known URLs return 401/404,
        but the host root publishes a single OAuth metadata document.
        """
        meta = {
            "issuer": "https://mcp.example.com",
            "authorization_endpoint": "https://mcp.example.com/authorize",
            "token_endpoint": "https://mcp.example.com/token",
            "code_challenge_methods_supported": ["S256"],
        }
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            host_root = MagicMock()
            host_root.status_code = 200
            host_root.json.return_value = meta
            # Three path-aware attempts fail, then the host-level RFC 8414 URL succeeds.
            mock_get.side_effect = [not_found, not_found, not_found, host_root]

            result = discover_auth_server_metadata("https://mcp.example.com/v1/mcp")
            assert result["token_endpoint"] == "https://mcp.example.com/token"
            assert result["code_challenge_methods_supported"] == ["S256"]
            assert result["_discovery_source"] == "rfc8414"
            # Fourth attempt should be the host-level RFC 8414 URL.
            assert mock_get.call_args_list[3][0][0] == "https://mcp.example.com/.well-known/oauth-authorization-server"


# ---------------------------------------------------------------------------
# _validate_resource_metadata (RFC 9728 §2, §3.3)
# ---------------------------------------------------------------------------

ATLASSIAN_AS = "https://auth.atlassian.com/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3"
ATLASSIAN_MCP = "https://mcp.atlassian.com/v2/mcp"
ATLASSIAN_PRM = "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp"


def _validate(resource_meta, auth_server_url, server_url, hops=None):
    """Call the validator with the MCP server's own path-aware PRM URL by default."""
    if hops is None:
        parsed = urllib.parse.urlparse(server_url)
        hops = [f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"]
    _validate_resource_metadata(resource_meta, hops, auth_server_url, server_url)


class TestValidateResourceMetadata:
    """Prevent redirect attacks via crafted Protected Resource Metadata."""

    # --- PRM without `resource`: the AS must share the MCP server's hostname ---

    def test_no_resource_same_host_passes(self):
        _validate({}, "https://example.com/oauth2", "https://example.com/mcp")

    def test_no_resource_same_host_different_port_passes(self):
        """Port differs but hostname matches — allowed."""
        _validate({}, "https://example.com:8443/oauth", "https://example.com/mcp")

    def test_no_resource_case_insensitive(self):
        _validate({}, "https://Example.COM/oauth", "https://example.com/mcp")

    def test_no_resource_different_host_raises(self):
        with pytest.raises(OAuthError, match="no 'resource' field"):
            _validate({}, "https://evil.example.com/oauth", "https://mcp.example.com/mcp")

    def test_no_resource_attacker_redirect_raises(self):
        with pytest.raises(OAuthError, match="does not match"):
            _validate({}, "https://attacker.com", "https://legit-server.com/mcp")

    def test_invalid_url_raises(self):
        with pytest.raises(OAuthError, match="Invalid authorization server"):
            _validate({}, "not-a-url", "https://example.com/mcp")

    # --- matching `resource`, PRM served by the MCP server's host over https ---

    def test_matching_resource_allows_cross_host_as(self):
        """Atlassian Rovo MCP v2's live metadata: PRM on mcp.atlassian.com, AS on auth.atlassian.com."""
        _validate({"resource": ATLASSIAN_MCP}, ATLASSIAN_AS, ATLASSIAN_MCP, hops=[ATLASSIAN_PRM])

    def test_matching_resource_ignores_case_default_port_trailing_slash_and_request_query(self):
        _validate(
            {"resource": ATLASSIAN_MCP}, ATLASSIAN_AS,
            "HTTPS://MCP.Atlassian.com:443/v2/mcp/?tools=all", hops=[ATLASSIAN_PRM],
        )

    def test_same_host_redirect_chain_allows_cross_host_as(self):
        _validate(
            {"resource": ATLASSIAN_MCP}, ATLASSIAN_AS, ATLASSIAN_MCP,
            hops=[ATLASSIAN_PRM, "https://mcp.atlassian.com/prm", "https://MCP.atlassian.com/prm2"],
        )

    def test_loopback_server_allows_loopback_http_prm_and_as(self):
        _validate(
            {"resource": "http://localhost:8000/mcp"}, "http://127.0.0.1:9000/oauth",
            "http://localhost:8000/mcp",
        )

    # --- matching `resource`, PRM NOT bound to the MCP server ---

    def test_foreign_host_https_prm_rejects_cross_host_as(self):
        """`resource` is self-asserted: an attacker's PRM can claim the legit URL."""
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate(
                {"resource": "https://legit.example.com/mcp"},
                "https://evil.attacker.com/oauth",
                "https://legit.example.com/mcp",
                hops=["https://evil.attacker.com/prm"],
            )

    def test_redirect_through_foreign_host_rejects_cross_host_as(self):
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate(
                {"resource": ATLASSIAN_MCP}, ATLASSIAN_AS, ATLASSIAN_MCP,
                hops=[ATLASSIAN_PRM, "https://evil.attacker.com/hop", ATLASSIAN_PRM],
            )

    def test_plaintext_hop_rejects_cross_host_as(self):
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate(
                {"resource": ATLASSIAN_MCP}, ATLASSIAN_AS, ATLASSIAN_MCP,
                hops=[ATLASSIAN_PRM, "http://mcp.atlassian.com/prm"],
            )

    def test_plaintext_mcp_server_rejects_cross_host_as(self):
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate(
                {"resource": "http://mcp.example.com/mcp"},
                "https://evil.attacker.com/oauth",
                "http://mcp.example.com/mcp",
            )

    def test_loopback_http_prm_for_non_loopback_server_rejects_cross_host_as(self):
        """Loopback is trusted only for a loopback MCP server."""
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate(
                {"resource": "https://mcp.example.com/mcp"},
                "https://auth.example.org/oauth",
                "https://mcp.example.com/mcp",
                hops=["http://localhost/prm"],
            )

    def test_no_hops_rejects_cross_host_as(self):
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate({"resource": ATLASSIAN_MCP}, ATLASSIAN_AS, ATLASSIAN_MCP, hops=[])

    def test_unbound_prm_still_allows_same_host_as(self):
        _validate(
            {"resource": "https://mcp.example.com/mcp"},
            "https://mcp.example.com/oauth",
            "https://mcp.example.com/mcp",
            hops=["https://elsewhere.example.org/prm"],
        )

    # --- mismatched `resource` is refused whatever the AS host ---

    def test_mismatched_resource_path_raises(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate(
                {"resource": "https://mcp.atlassian.com/v1/mcp/authv2"},
                ATLASSIAN_AS, ATLASSIAN_MCP, hops=[ATLASSIAN_PRM],
            )

    def test_mismatched_resource_host_raises(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate(
                {"resource": "https://attacker.com/mcp"},
                "https://attacker.com/oauth",
                "https://legit-server.com/mcp",
            )

    def test_root_only_prm_with_bare_origin_resource_allows_same_host_as(self):
        """Shape of Notion's live root document: murl fell back to it from the path-aware URL."""
        _validate(
            {"resource": "https://mcp.notion.com"},
            "https://mcp.notion.com",
            "https://mcp.notion.com/mcp",
            hops=["https://mcp.notion.com/.well-known/oauth-protected-resource"],
        )

    def test_root_only_prm_with_bare_origin_resource_rejects_cross_host_as(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate(
                {"resource": "https://mcp.notion.com"},
                "https://auth.example.org/oauth",
                "https://mcp.notion.com/mcp",
                hops=["https://mcp.notion.com/.well-known/oauth-protected-resource"],
            )

    def test_mismatched_resource_allows_same_host_as(self):
        _validate(
            {"resource": "https://example.com/other"},
            "https://example.com/oauth2",
            "https://example.com/mcp",
        )

    def test_mismatched_resource_port_raises(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate(
                {"resource": "https://example.com:8443/mcp"}, ATLASSIAN_AS, "https://example.com/mcp",
            )

    def test_mismatched_resource_scheme_raises(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate({"resource": "http://example.com/mcp"}, ATLASSIAN_AS, "https://example.com/mcp")

    def test_mismatched_resource_scheme_same_explicit_port_raises(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate(
                {"resource": "http://example.com:8443/mcp"}, ATLASSIAN_AS,
                "https://example.com:8443/mcp",
            )

    def test_only_one_trailing_slash_is_ignored(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate({"resource": "https://example.com/mcp//"}, ATLASSIAN_AS, "https://example.com/mcp")

    def test_resource_query_must_equal_request_query(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate(
                {"resource": "https://example.com/mcp?tenant=a"}, ATLASSIAN_AS,
                "https://example.com/mcp?tenant=b",
            )

    def test_resource_query_matches_identical_request_query(self):
        _validate(
            {"resource": "https://example.com/mcp?tenant=a"}, "https://example.com/oauth",
            "https://example.com/mcp?tenant=a",
        )

    def test_malformed_ipv6_resource_is_a_mismatch(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate({"resource": "https://[x/mcp"}, ATLASSIAN_AS, "https://example.com/mcp")

    @pytest.mark.parametrize("as_url", ["https://[x/oauth", 123, None])
    def test_malformed_or_non_string_as_url_raises_oauth_error(self, as_url):
        with pytest.raises(OAuthError, match="Invalid authorization server"):
            _validate({"resource": ATLASSIAN_MCP}, as_url, ATLASSIAN_MCP, hops=[ATLASSIAN_PRM])

    def test_malformed_hop_rejects_cross_host_as(self):
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate(
                {"resource": ATLASSIAN_MCP}, ATLASSIAN_AS, ATLASSIAN_MCP,
                hops=[ATLASSIAN_PRM, "https://[x/prm"],
            )

    def test_hostless_server_and_hop_reject_cross_host_as(self):
        """An empty hostname never counts as the MCP server's host."""
        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            _validate({"resource": "https:///mcp"}, ATLASSIAN_AS, "https:///mcp", hops=["https:///prm"])

    def test_unparseable_resource_port_is_a_mismatch(self):
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate({"resource": "https://example.com:99999/mcp"}, ATLASSIAN_AS, "https://example.com/mcp")

    @pytest.mark.parametrize("resource", [None, 123, ["https://example.com/mcp"], ""])
    def test_non_string_or_empty_resource_raises(self, resource):
        """A present `resource` that is not the requested URL never passes.

        JSON null counts as present: it is a value, not an absent field.
        """
        with pytest.raises(OAuthError, match="RFC 9728"):
            _validate({"resource": resource}, ATLASSIAN_AS, "https://example.com/mcp")

    # --- AS scheme ---

    def test_http_as_raises(self):
        with pytest.raises(OAuthError, match="must use https"):
            _validate({"resource": "https://example.com/mcp"}, "http://auth.example.com", "https://example.com/mcp")

    def test_http_same_host_as_raises(self):
        with pytest.raises(OAuthError, match="must use https"):
            _validate({}, "http://example.com/oauth", "https://example.com/mcp")

    @pytest.mark.parametrize("as_url", [
        "http://localhost:9000/oauth", "http://127.0.0.1:9000", "http://[::1]:9000",
    ])
    def test_http_loopback_as_passes_for_loopback_server(self, as_url):
        _validate({"resource": "http://localhost:8000/mcp"}, as_url, "http://localhost:8000/mcp")

    def test_http_loopback_as_raises_for_non_loopback_server(self):
        with pytest.raises(OAuthError, match="must use https"):
            _validate({}, "http://localhost:9000/oauth", "https://example.com/mcp")

    @pytest.mark.parametrize("as_url", ["ftp://example.com/oauth", "ftp://localhost/oauth"])
    def test_non_http_scheme_as_raises(self, as_url):
        """Only http gets the loopback exemption; other schemes never pass."""
        with pytest.raises(OAuthError, match="must use https"):
            _validate({"resource": "http://localhost/mcp"}, as_url, "http://localhost/mcp")


# ---------------------------------------------------------------------------
# canonical_resource_uri
# ---------------------------------------------------------------------------

class TestCanonicalResourceUri:
    """MCP 2025-11-25 §Resource Parameter Implementation / RFC 8707 §2.

    The canonical URI MUST NOT include fragments, SHOULD omit trailing slash
    unless semantically significant, and SHOULD be the most specific URI.
    """

    def test_simple(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"

    def test_strips_fragment(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp#frag") == "https://mcp.example.com/mcp"

    def test_strips_query(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp?foo=bar") == "https://mcp.example.com/mcp"

    def test_no_path_no_trailing_slash(self):
        """Spec SHOULD: use form without trailing slash (MCP 2025-11-25 §Canonical Server URI)."""
        assert _canonical_resource_uri("https://mcp.example.com") == "https://mcp.example.com"

    def test_preserves_explicit_path_slash(self):
        """A trailing slash that was explicitly part of the URL is preserved."""
        assert _canonical_resource_uri("https://mcp.example.com/") == "https://mcp.example.com/"

    def test_with_port(self):
        """Port is part of the canonical URI (spec example: https://mcp.example.com:8443)."""
        assert _canonical_resource_uri("https://mcp.example.com:8443/mcp") == "https://mcp.example.com:8443/mcp"


# ---------------------------------------------------------------------------
# Full authorize flow (mocked)
# ---------------------------------------------------------------------------

class TestAuthorize:
    """MCP 2025-11-25 §Authorization Flow Steps — end-to-end flow tests.

    Validates: discovery chain, PKCE enforcement, resource parameter (RFC 8707),
    client registration priority, and scope selection strategy.
    """

    def _mock_full_flow(self, mock_httpx_get, mock_httpx_post, mock_webbrowser, mock_server):
        """Set up mocks for a successful full OAuth flow."""
        # Metadata discovery
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
            "code_challenge_methods_supported": ["S256"],
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
        assert creds["resource_uri"] == "https://example.com/mcp"

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
        # RFC 8707: resource parameter must be present
        assert params["resource"] == ["https://example.com/mcp"]

        # Verify token exchange also included resource parameter
        token_call = mock_post.call_args_list[1]  # second POST is token exchange
        token_data = token_call[1].get("data", {})
        assert token_data["resource"] == "https://example.com/mcp"

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
            "code_challenge_methods_supported": ["S256"],
            # No registration_endpoint
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="registration endpoint"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_pkce_missing_from_metadata(self, mock_get, mock_post, mock_browser, mock_server):
        """authorize() must refuse if code_challenge_methods_supported is absent."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
            # No code_challenge_methods_supported
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="does not advertise PKCE support"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_pkce_oidc_missing_proceeds_with_warning(self, mock_get, mock_post, mock_browser, mock_server):
        """OIDC metadata without code_challenge_methods_supported should warn but proceed.

        OIDC Provider Metadata does not define this field (it's an OAuth 2.0 AS
        Metadata field).  Major IDPs like Okta fully support S256 PKCE but don't
        include the field in their OIDC discovery document.
        """
        # Resource metadata -> points to auth server (same origin as MCP server)
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "authorization_servers": ["https://example.com/oauth2/default"],
        }

        # Auth server metadata via OIDC (no code_challenge_methods_supported)
        oidc_resp = MagicMock()
        oidc_resp.status_code = 200
        oidc_resp.json.return_value = {
            "issuer": "https://example.com/oauth2/default",
            "authorization_endpoint": "https://example.com/oauth2/default/v1/authorize",
            "token_endpoint": "https://example.com/oauth2/default/v1/token",
            "registration_endpoint": "https://example.com/oauth2/v1/clients",
            # No code_challenge_methods_supported — typical for Okta OIDC
        }

        # RFC 8414 fails (404/405), OIDC path-insert fails, OIDC path-append succeeds
        not_found = MagicMock()
        not_found.status_code = 405

        # Order: resource_meta path-aware (succeeds), AS rfc8414, OIDC insert, OIDC append
        mock_get.side_effect = [resource_meta_resp, not_found, not_found, oidc_resp]

        # Registration + token exchange
        reg_resp = MagicMock()
        reg_resp.status_code = 201
        reg_resp.json.return_value = {"client_id": "cid_oidc"}

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_oidc",
            "refresh_token": "rt_oidc",
            "expires_in": 3600,
        }
        mock_post.side_effect = [reg_resp, token_resp]
        mock_browser.return_value = True
        mock_server.return_value = "test_auth_code"

        # Should succeed despite missing code_challenge_methods_supported
        creds = authorize("https://example.com/mcp")
        assert creds["access_token"] == "at_oidc"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_pkce_s256_not_supported(self, mock_get, mock_post, mock_browser, mock_server):
        """authorize() must refuse if S256 is not in code_challenge_methods_supported."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
            "code_challenge_methods_supported": ["plain"],
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="does not support S256"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_preconfigured_credentials_skip_registration(self, mock_get, mock_post, mock_browser, mock_server):
        """Pre-configured client_id skips Dynamic Client Registration."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "code_challenge_methods_supported": ["S256"],
            # No registration_endpoint — should still work with pre-configured creds
        }
        mock_get.return_value = meta_resp

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_preconfig",
            "refresh_token": "rt_preconfig",
            "expires_in": 3600,
        }
        mock_post.return_value = token_resp
        mock_browser.return_value = True
        mock_server.return_value = "test_auth_code"

        creds = authorize(
            "https://example.com/mcp",
            client_id="my-app-id",
            client_secret="my-app-secret",
            callback_port=8080,
        )

        assert creds["access_token"] == "at_preconfig"
        assert creds["client_id"] == "my-app-id"

        # Only one POST (token exchange) — no registration POST
        assert mock_post.call_count == 1

        # Verify the auth URL uses the pre-configured client_id and fixed port
        auth_url = mock_browser.call_args[0][0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        assert params["client_id"] == ["my-app-id"]
        assert params["redirect_uri"] == ["http://localhost:8080/callback"]

        # Verify token exchange includes client_secret
        token_data = mock_post.call_args[1].get("data", {})
        assert token_data["client_secret"] == "my-app-secret"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_no_registration_and_no_client_id_raises(self, mock_get, mock_post, mock_browser, mock_server):
        """Without pre-configured creds or a registration endpoint, authorize fails."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "code_challenge_methods_supported": ["S256"],
            # No registration_endpoint
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="--client-id"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_cross_host_auth_server_with_matching_resource_proceeds(
        self, mock_get, mock_post, mock_browser, mock_server
    ):
        """RFC 9728 §3.3: a `resource` naming the requested URL binds a cross-host AS.

        Shape of Atlassian Rovo MCP v2's live metadata.
        """
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "resource": "https://mcp.atlassian.com/v2/mcp",
            "authorization_servers": [ATLASSIAN_AS],
        }
        as_meta_resp = MagicMock()
        as_meta_resp.status_code = 200
        as_meta_resp.json.return_value = {
            "issuer": ATLASSIAN_AS,
            "authorization_endpoint": f"{ATLASSIAN_AS}/authorize",
            "token_endpoint": f"{ATLASSIAN_AS}/token",
            "registration_endpoint": f"{ATLASSIAN_AS}/register",
            "code_challenge_methods_supported": ["S256"],
        }
        mock_get.side_effect = [resource_meta_resp, as_meta_resp]

        reg_resp = MagicMock()
        reg_resp.status_code = 201
        reg_resp.json.return_value = {"client_id": "cid_atl"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_atl", "refresh_token": "rt_atl", "expires_in": 3600,
        }
        mock_post.side_effect = [reg_resp, token_resp]
        mock_browser.return_value = True
        mock_server.return_value = "code"

        creds = authorize("https://mcp.atlassian.com/v2/mcp")

        assert creds["access_token"] == "at_atl"
        assert creds["token_endpoint"] == f"{ATLASSIAN_AS}/token"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_root_prm_fallback_with_bare_origin_resource_proceeds_same_host(
        self, mock_get, mock_post, mock_browser, mock_server
    ):
        """Path-aware PRM 404s, the root document names the bare origin, the AS is same-host."""
        not_found = MagicMock()
        not_found.status_code = 404
        root_prm = MagicMock()
        root_prm.status_code = 200
        root_prm.json.return_value = {
            "resource": "https://mcp.example.com",
            "authorization_servers": ["https://mcp.example.com"],
        }
        as_meta = MagicMock()
        as_meta.status_code = 200
        as_meta.json.return_value = {
            "issuer": "https://mcp.example.com",
            "authorization_endpoint": "https://mcp.example.com/authorize",
            "token_endpoint": "https://mcp.example.com/token",
            "registration_endpoint": "https://mcp.example.com/register",
            "code_challenge_methods_supported": ["S256"],
        }
        mock_get.side_effect = [not_found, root_prm, as_meta]
        reg_resp = MagicMock()
        reg_resp.status_code = 201
        reg_resp.json.return_value = {"client_id": "cid"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        mock_post.side_effect = [reg_resp, token_resp]
        mock_browser.return_value = True
        mock_server.return_value = "code"

        creds = authorize("https://mcp.example.com/mcp")

        assert mock_get.call_args_list[1][0][0] == "https://mcp.example.com/.well-known/oauth-protected-resource"
        assert creds["access_token"] == "at"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_foreign_prm_via_www_authenticate_rejected_before_any_auth_request(
        self, mock_get, mock_post, mock_browser, mock_server
    ):
        """An attacker-hosted https PRM claiming the legit resource cannot pick the AS."""
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "resource": "https://legit-server.com/mcp",
            "authorization_servers": ["https://evil.attacker.com/oauth"],
        }
        mock_get.return_value = resource_meta_resp

        with pytest.raises(OAuthError, match="not served by the MCP server's host"):
            authorize(
                "https://legit-server.com/mcp",
                www_authenticate='Bearer resource_metadata="https://evil.attacker.com/prm"',
            )
        assert mock_get.call_args_list[0][0][0] == "https://evil.attacker.com/prm"
        assert mock_get.call_count == 1
        assert not mock_post.called
        assert not mock_browser.called

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_mismatched_resource_rejected_before_any_auth_request(
        self, mock_get, mock_post, mock_browser, mock_server
    ):
        """RFC 9728 §3.3: metadata for another resource is refused outright."""
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "resource": "https://evil.attacker.com/mcp",
            "authorization_servers": ["https://evil.attacker.com/oauth"],
        }
        mock_get.return_value = resource_meta_resp

        with pytest.raises(OAuthError, match="RFC 9728"):
            authorize("https://legit-server.com/mcp")
        assert mock_get.call_count == 1
        assert not mock_post.called
        assert not mock_browser.called

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_cross_origin_auth_server_rejected(self, mock_get, mock_post, mock_browser, mock_server):
        """No `resource` in the metadata: reject an AS on a different hostname."""
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "authorization_servers": ["https://evil.attacker.com/oauth"],
        }
        mock_get.return_value = resource_meta_resp

        with pytest.raises(OAuthError, match="does not match"):
            authorize("https://legit-server.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_cross_origin_auth_server_allowed_with_client_id(self, mock_get, mock_post, mock_browser, mock_server):
        """Pre-registered credentials bypass the same-origin check.

        Deployments like AWS AgentCore + Okta intentionally split the resource
        server (AgentCore, *.amazonaws.com) and the auth server (Okta,
        *.okta.com) across different domains. When the caller supplies
        --client-id they have established the trust relationship out-of-band,
        so the cross-domain check would only block legitimate usage.
        """
        # Resource server: agentcore.amazonaws.com
        # Auth server:     example.oktapreview.com — different domain
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "authorization_servers": ["https://example.oktapreview.com/oauth2/default"],
        }

        as_meta_resp = MagicMock()
        as_meta_resp.status_code = 200
        as_meta_resp.json.return_value = {
            "issuer": "https://example.oktapreview.com/oauth2/default",
            "authorization_endpoint": "https://example.oktapreview.com/oauth2/default/v1/authorize",
            "token_endpoint": "https://example.oktapreview.com/oauth2/default/v1/token",
            "code_challenge_methods_supported": ["S256"],
        }

        mock_get.side_effect = [resource_meta_resp, as_meta_resp]

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_agentcore",
            "refresh_token": "rt_agentcore",
            "expires_in": 3600,
        }
        mock_post.return_value = token_resp
        mock_browser.return_value = True
        mock_server.return_value = "test_auth_code"

        from unittest.mock import patch as _patch

        # Should NOT raise OAuthError despite cross-domain auth server,
        # but MUST emit a warning naming the unvalidated issuer.
        with _patch("click.echo") as mock_echo:
            creds = authorize(
                "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3A.../invocations",
                client_id="0oaEXAMPLECLIENT02",
                client_secret="my-secret",
                callback_port=9999,
            )
        assert creds["access_token"] == "at_agentcore"
        # Verify the warning was emitted and names the issuer
        warning_calls = [
            str(call) for call in mock_echo.call_args_list
            if "skipping resource metadata checks" in str(call)
        ]
        assert warning_calls, "Expected a metadata-check-skip warning to be printed"
        assert "oktapreview.com" in warning_calls[0]


# ---------------------------------------------------------------------------
# Callback port selection (issue #10)
# ---------------------------------------------------------------------------

class TestSelectCallbackPort:
    """_select_callback_port: default to 8080, honor override, fail fast on conflict."""

    def test_defaults_to_standard_8080_when_unset(self):
        # No callback_port supplied → the standard SPA redirect-URI port.
        # Patch the probe so the test doesn't depend on 8080 being free in CI.
        with patch("murl.auth.socket.socket") as mock_sock:
            mock_sock.return_value.__enter__.return_value.bind = MagicMock()
            port = _select_callback_port(None)
        assert port == STANDARD_OAUTH_CALLBACK_PORT == 8080

    def test_explicit_port_takes_precedence_over_default(self):
        with patch("murl.auth.socket.socket") as mock_sock:
            mock_sock.return_value.__enter__.return_value.bind = MagicMock()
            port = _select_callback_port(9123)
        assert port == 9123

    def test_probes_the_selected_port(self):
        # The probe must bind the chosen port (not an ephemeral 0).
        with patch("murl.auth.socket.socket") as mock_sock:
            bind = mock_sock.return_value.__enter__.return_value.bind
            _select_callback_port(8080)
        bind.assert_called_once_with(("localhost", 8080))

    def test_port_in_use_raises_actionable_oautherror(self):
        # Occupy a real ephemeral port, then ask the selector to use it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("localhost", 0))
            occupied.listen(1)
            taken = occupied.getsockname()[1]

            with pytest.raises(OAuthError) as exc_info:
                _select_callback_port(taken)

        err = exc_info.value
        assert str(taken) in str(err)
        assert "already in use" in str(err)
        # Suggestion is failure-specific: point at --callback-port, not --login.
        assert err.suggestion is not None
        assert "--callback-port" in err.suggestion


class TestOAuthErrorSuggestion:
    """OAuthError carries an optional failure-specific suggestion."""

    def test_default_suggestion_is_none(self):
        err = OAuthError("boom")
        assert str(err) == "boom"
        assert err.suggestion is None

    def test_suggestion_is_preserved(self):
        err = OAuthError("boom", suggestion="do the thing")
        assert err.suggestion == "do the thing"
