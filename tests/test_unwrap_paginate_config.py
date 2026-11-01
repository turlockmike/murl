"""Unit tests for unwrap, paginate, auth-failure detection, and .mcp.json defaults."""

import json
import os
from pathlib import Path

import pytest

from murl.cli import (
    UPGRADE_SOURCE,
    detect_tool_auth_failure,
    McpContent,
    find_mcp_config,
    mcp_config_defaults,
    unwrap_text_envelope,
)


def test_upgrade_source_keeps_optional_extras():
    # An upgrade must not drop the keychain and TOON extras.
    assert UPGRADE_SOURCE == "mcp-curl[keychain,toon]"


# --- unwrap_text_envelope ---


def test_unwrap_text_envelope_with_json_text():
    inner = {"hello": "world", "items": [1, 2]}
    envelope = [{"type": "text", "text": json.dumps(inner)}]
    result, did = unwrap_text_envelope(envelope)
    assert did is True
    assert result == inner


def test_unwrap_text_envelope_passes_through_non_envelope():
    # Not a single-item list
    result, did = unwrap_text_envelope([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert did is False
    assert result == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


def test_unwrap_text_envelope_passes_through_non_json_text():
    envelope = [{"type": "text", "text": "this is plain text, not JSON"}]
    result, did = unwrap_text_envelope(envelope)
    assert did is False
    assert result == envelope


def test_unwrap_text_envelope_ignores_non_text_blocks():
    envelope = [{"type": "image", "data": "..."}]
    result, did = unwrap_text_envelope(envelope)
    assert did is False
    assert result == envelope


def test_unwrap_text_envelope_handles_non_list():
    result, did = unwrap_text_envelope({"already": "unwrapped"})
    assert did is False
    assert result == {"already": "unwrapped"}


# --- detect_tool_auth_failure ---
# An auth failure a server returns as a successful result. Only a whole value
# at the top level counts; everything else is content.


@pytest.mark.parametrize("body", [
    # A server's inbound-bearer rejection.
    {"error": "unauthorized", "reason": "verify-failed", "tool": "gmail_get_message"},
    {"error": "invalid_token"},
    {"error": "  Invalid_Token "},
    {"error": {"code": "invalid_token", "message": "JWT expired"}},
    {"code": "UNAUTHENTICATED"},
    {"status": "AUTH_FAILED", "reason": "OBO exchange failed"},
    {"code": "token_expired", "message": "refresh the token"},
    # A Google API failure returned as a result: {error, status}.
    {"error": "Request had invalid authentication credentials.", "status": 401},
    {"statusCode": 403, "message": "Forbidden"},
    {"status": "ok", "statusCode": 401},
    {"title": "Unauthorized", "status": 401, "type": "about:blank"},
])
def test_detect_tool_auth_failure_top_level_auth_value(body):
    assert detect_tool_auth_failure(body) is not None


def test_detect_tool_auth_failure_description_carries_the_server_detail():
    assert detect_tool_auth_failure(
        {"error": "unauthorized", "reason": "verify-failed"}) == "unauthorized: verify-failed"
    assert detect_tool_auth_failure(
        {"error": {"code": "invalid_token", "message": "JWT expired"}}) == "invalid_token: JWT expired"
    assert detect_tool_auth_failure({"status": 401, "error": "bad creds"}) == "status 401: bad creds"
    assert detect_tool_auth_failure({"error": "invalid_token"}) == "invalid_token"
    assert detect_tool_auth_failure(
        {"code": "unauthorized", "message": "sign in again"}) == "unauthorized: sign in again"


@pytest.mark.parametrize("body", [
    # The reported case: a search hit that quotes the word.
    {"items": [{"ref": {"session": "s1"}, "score": 0.5,
                "text": "User: the API said 401 Unauthorized when I tried"}]},
    # A word inside a longer value is content.
    {"error": "the reviewer flagged unauthorized access"},
    {"message": "Unauthorized"},
    {"code": "raise Unauthorized()"},
    {"title": "Unauthorized access report"},
    # Nested and listed values are content.
    {"result": {"status": "Unauthorized"}},
    {"data": {"error": "invalid_token"}},
    {"errors": [{"message": "Unauthorized", "code": "invalid_token"}]},
    {"items": [{"error": "unauthorized"}]},
    {"error": {"message": "invalid_token"}},
    # Status values that are not an integer 401/403.
    {"status": "401"},
    {"status": True},
    {"status": 404, "error": "not found"},
    {"status": 401.0},
    # Codes outside the set.
    {"error": "forbidden_resource"},
    {"code": "E1"},
    {"error": None, "code": 401},
    {"error": ["unauthorized"]},
    {"statusCode": "401"},
])
def test_detect_tool_auth_failure_content_is_not_matched(body):
    assert detect_tool_auth_failure(body) is None


@pytest.mark.parametrize("text,hit", [
    ("401 Unauthorized", True),
    ("401 Unauthorised", True),
    ("HTTP 401", True),
    ("403 Forbidden", True),
    ("401", False),
    ("401 users migrated", False),
    ("403 items found", False),
    ("HTTP/1.1 403 Forbidden", True),
    ("http 401 unauthorized", True),
    ("  invalid_token\n", True),
    ("Unauthorized", True),
    ("authentication failed for user", False),
    ("the API said 401 Unauthorized when I tried", False),
    ("4010 records", False),
    ("401 Unauthorized: token for alice expired at 12:00", False),
    ("", False),
])
def test_detect_tool_auth_failure_text_result(text, hit):
    assert (detect_tool_auth_failure(text) is not None) is hit


def test_detect_tool_auth_failure_plain_text_content_blocks():
    # Text that is not JSON is not unwrapped, so make_mcp_request returns the
    # McpContent it built. Run it through the real unwrap to get that shape.
    blocks = McpContent([{"type": "text", "text": "401 Unauthorized"}])
    result, did = unwrap_text_envelope(blocks)
    assert did is False
    assert isinstance(result, McpContent)
    assert detect_tool_auth_failure(result) is not None
    two = McpContent([{"type": "text", "text": "request failed"},
                      {"type": "text", "text": "invalid_token"}])
    assert detect_tool_auth_failure(two) is not None
    assert detect_tool_auth_failure(McpContent([{"type": "text", "text": "all good"}])) is None
    quoted = McpContent([{"type": "text", "text": "they said 401 Unauthorized"},
                         {"type": "text", "text": "more"}])
    assert detect_tool_auth_failure(quoted) is None
    assert detect_tool_auth_failure(McpContent([{"type": "image", "data": "Unauthorized"}])) is None
    assert detect_tool_auth_failure(McpContent(["Unauthorized", {"type": []}])) is None


def test_detect_tool_auth_failure_mcp_content_json_blocks_use_object_rule():
    hits = json.dumps({"hits": [{"text": "they said Unauthorized"}]})
    blocks = McpContent([{"type": "text", "text": hits}, {"type": "text", "text": '{"a": 1}'}])
    assert detect_tool_auth_failure(blocks) is None
    err = McpContent([{"type": "text", "text": '{"error": "invalid_token"}'},
                      {"type": "text", "text": '{"a": 1}'}])
    assert detect_tool_auth_failure(err) is not None
    # A block that is a JSON string is text.
    jstr = McpContent([{"type": "text", "text": '"Unauthorized"'}, {"type": "text", "text": "x"}])
    assert detect_tool_auth_failure(jstr) is not None
    # A block that is a JSON list or number is content.
    other = McpContent([{"type": "text", "text": '["Unauthorized"]'},
                        {"type": "text", "text": "401.5"}])
    assert detect_tool_auth_failure(other) is None


@pytest.mark.parametrize("payload", [
    # Parsed result lists are content, whatever their items hold.
    [{"type": "message", "text": "they said Unauthorized"}, {"type": "message", "text": "ok"}],
    [{"error": "unauthorized"}],
    ["Unauthorized"],
])
def test_detect_tool_auth_failure_parsed_list_is_content(payload):
    result, did = unwrap_text_envelope(McpContent([{"type": "text", "text": json.dumps(payload)}]))
    assert did is True
    assert not isinstance(result, McpContent)
    assert detect_tool_auth_failure(result) is None


def test_detect_tool_auth_failure_mcp_content_scans_first_50_blocks():
    filler = [{"type": "text", "text": "ok"}] * 50
    late = McpContent(filler + [{"type": "text", "text": "401 Unauthorized"}])
    assert detect_tool_auth_failure(late) is None
    early = McpContent(filler[:49] + [{"type": "text", "text": "401 Unauthorized"}])
    assert detect_tool_auth_failure(early) is not None


def test_detect_tool_auth_failure_mcp_content_deeply_nested_json_is_text():
    deep = "[" * 200_000
    blocks = McpContent([{"type": "text", "text": deep}, {"type": "text", "text": "ok"}])
    assert detect_tool_auth_failure(blocks) is None


# --- find_mcp_config + mcp_config_defaults ---


def test_find_mcp_config_walks_up(tmp_path, monkeypatch):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"foo": {"url": "https://example/mcp", "oauth": {"clientId": "abc"}}}}))
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = find_mcp_config()
    assert result is not None
    assert "mcpServers" in result


def test_find_mcp_config_missing_returns_none(tmp_path, monkeypatch):
    sub = tmp_path / "nested"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setenv("HOME", str(tmp_path))  # stop walk at tmp root quickly
    assert find_mcp_config() is None


def test_find_mcp_config_malformed_returns_none(tmp_path, monkeypatch):
    (tmp_path / ".mcp.json").write_text("{ not valid json")
    monkeypatch.chdir(tmp_path)
    assert find_mcp_config() is None


def test_mcp_config_defaults_exact_match():
    config = {
        "mcpServers": {
            "example": {
                "url": "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3A.../invocations?qualifier=DEFAULT",
                "oauth": {"clientId": "0oaEXAMPLECLIENT01", "callbackPort": 8080},
            }
        }
    }
    # The base URL passed in must decode to the same string as the config entry.
    base_url = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn:.../invocations?qualifier=DEFAULT"
    defaults = mcp_config_defaults(base_url, config)
    assert defaults["client_id"] == "0oaEXAMPLECLIENT01"
    assert defaults["callback_port"] == 8080


def test_mcp_config_defaults_trailing_slash_normalized():
    config = {"mcpServers": {"s": {"url": "https://x.example/mcp/", "oauth": {"clientId": "abc"}}}}
    assert mcp_config_defaults("https://x.example/mcp", config) == {"client_id": "abc"}


def test_mcp_config_defaults_partial_match_rejected():
    # A prefix match must NOT silently use the wrong config — caller URL has
    # an extra path segment the config entry does not.
    config = {"mcpServers": {"s": {"url": "https://x.example/mcp", "oauth": {"clientId": "abc"}}}}
    assert mcp_config_defaults("https://x.example/mcp/extra", config) == {}


def test_mcp_config_defaults_query_mismatch_rejected():
    # Different qualifier query strings refer to different runtimes; do not
    # silently fall through to the wrong entry.
    config = {"mcpServers": {"s": {"url": "https://x.example/mcp?qualifier=PROD", "oauth": {"clientId": "abc"}}}}
    assert mcp_config_defaults("https://x.example/mcp?qualifier=STAGING", config) == {}


def test_mcp_config_defaults_no_match():
    config = {"mcpServers": {"other": {"url": "https://other.example/mcp", "oauth": {"clientId": "x"}}}}
    defaults = mcp_config_defaults("https://nope.example/mcp", config)
    assert defaults == {}


def test_mcp_config_defaults_none_config():
    assert mcp_config_defaults("https://anywhere/mcp", None) == {}


def test_mcp_config_defaults_missing_oauth_block():
    config = {"mcpServers": {"x": {"url": "https://x.example/mcp"}}}
    assert mcp_config_defaults("https://x.example/mcp", config) == {}
