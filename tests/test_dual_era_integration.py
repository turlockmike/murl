"""Integration tests for murl against both MCP protocol revisions.

Two local servers, one per revision, both driven through murl's real CLI:

* ``mcp_test_server.py``      — 2025 era: ``initialize`` handshake, plain JSON.
* ``mcp_test_server_2026.py`` — 2026-07-28: no handshake, ``server/discover``,
  ``_meta`` envelope, ``Mcp-Method``/``Mcp-Name`` headers, ``resultType``.

Why both, locally: servers adopt the new revision one at a time, so murl
talks to a mix of revisions for the whole transition. Testing one revision cannot
distinguish a dual-era client from a single-era one — both pass. Running the
pair with no network, no Okta and no AWS makes that distinction cheap enough to
assert on every commit.

The ``legacy_server`` / ``modern_server`` fixtures live in ``conftest.py`` so new
test files can use them too. To drive the servers by hand instead:

    python tests/serve_test_server.py both
    murl http://localhost:8765/mcp tools/list --no-auth   # 2025
    murl http://localhost:8767/mcp tools/list --no-auth   # 2026-07-28
"""

import pytest
import requests
from click.testing import CliRunner

from murl.cli import main

from .conftest import MODERN_META, ndjson

# --- the servers themselves -------------------------------------------------
# These guard the instrument. A test proving murl correct is worthless if the
# server it proves murl against is not actually revision-conformant.


def test_modern_server_refuses_the_legacy_handshake(modern_server):
    """`initialize` is not part of 2026-07-28, so the server must reject it."""
    res = requests.post(
        modern_server,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        timeout=5,
    )
    assert res.json()["error"]["code"] == -32601


def test_legacy_server_answers_the_handshake(legacy_server):
    res = requests.post(
        legacy_server,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        timeout=5,
    )
    assert "result" in res.json()


@pytest.mark.parametrize(
    "headers,params,expected_code",
    [
        # Envelope: both keys are required on every modern request.
        (
            {"Mcp-Method": "tools/list"},
            {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            -32602,
        ),
        # `Mcp-Method` is required, and must agree with the body.
        ({}, {"_meta": MODERN_META}, -32020),
        ({"Mcp-Method": "tools/call"}, {"_meta": MODERN_META}, -32020),
    ],
)
def test_modern_server_enforces_the_revision(modern_server, headers, params, expected_code):
    res = requests.post(
        modern_server,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
        headers=headers,
        timeout=5,
    )
    assert res.json()["error"]["code"] == expected_code


def test_modern_server_requires_a_matching_mcp_name(modern_server):
    """`Mcp-Name` binds the methods that name a target."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "x"}, "_meta": MODERN_META},
    }
    absent = requests.post(modern_server, json=body, headers={"Mcp-Method": "tools/call"}, timeout=5)
    assert absent.json()["error"]["code"] == -32020

    wrong = requests.post(
        modern_server,
        json=body,
        headers={"Mcp-Method": "tools/call", "Mcp-Name": "weather"},
        timeout=5,
    )
    assert wrong.json()["error"]["code"] == -32020

    ok = requests.post(
        modern_server,
        json=body,
        headers={"Mcp-Method": "tools/call", "Mcp-Name": "echo"},
        timeout=5,
    )
    assert ok.json()["result"]["content"][0]["text"] == "x"
    # Every result on this revision carries `resultType`.
    assert ok.json()["result"]["resultType"] == "complete"


# --- a 2025-era server behind an error-rewriting proxy ----------------------
# Regression: servers behind Amazon Bedrock AgentCore. AgentCore collapses a runtime-side JSON-RPC error into a generic 400 — so the
# -32601 a 2025-era server sends for `server/discover` never reaches the client.
# Deciding the era from the error CODE therefore made murl unable to reach
# any AgentCore-hosted server, while the local 2025-era test above passed
# because nothing rewrote its errors.


def test_murl_reaches_a_server_whose_discover_error_was_rewritten(proxied_legacy_server):
    """The handshake must run even when the probe's error says nothing useful."""
    result = CliRunner().invoke(main, [proxied_legacy_server, "tools/list", "--no-auth"])

    assert result.exit_code == 0, result.output
    assert [t["name"] for t in ndjson(result.output)] == ["echo", "weather"]


def test_murl_calls_a_tool_through_the_rewriting_proxy(proxied_legacy_server):
    result = CliRunner().invoke(
        main, [proxied_legacy_server, "tools/call", "echo", "-d", "message=through proxy", "--no-auth"]
    )

    assert result.exit_code == 0, result.output
    assert "through proxy" in result.output


def test_the_proxy_fixture_really_hides_the_era_signal(proxied_legacy_server):
    """Guards the instrument: if the fixture leaked -32601 the tests above would
    pass for the wrong reason, proving nothing about the rewritten case."""
    res = requests.post(
        proxied_legacy_server,
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        timeout=5,
    )
    err = res.json()["error"]
    assert err["code"] == -32010, err
    assert "from runtime" in err["message"].lower(), err


# --- murl against each revision --------------------------------------------


def test_murl_lists_tools_on_the_legacy_server(legacy_server):
    result = CliRunner().invoke(main, [legacy_server, "tools/list", "--no-auth"])

    assert result.exit_code == 0, result.output
    assert [t["name"] for t in ndjson(result.output)] == ["echo", "weather"]


def test_murl_calls_a_tool_on_the_legacy_server(legacy_server):
    result = CliRunner().invoke(
        main, [legacy_server, "tools/call", "echo", "-d", "message=hello legacy", "--no-auth"]
    )

    assert result.exit_code == 0, result.output
    assert "hello legacy" in result.output


def test_murl_lists_tools_on_the_modern_server(modern_server):
    """murl must reach a server that offers no `initialize` handshake."""
    result = CliRunner().invoke(main, [modern_server, "tools/list", "--no-auth"])

    assert result.exit_code == 0, result.output
    assert [t["name"] for t in ndjson(result.output)] == ["echo", "weather"]


def test_murl_calls_a_tool_on_the_modern_server(modern_server):
    """A named method needs a matching `Mcp-Name`, so this fails if murl omits it."""
    result = CliRunner().invoke(
        main, [modern_server, "tools/call", "echo", "-d", "message=hello modern", "--no-auth"]
    )

    assert result.exit_code == 0, result.output
    assert "hello modern" in result.output


def test_murl_reports_a_tool_error_on_the_modern_server(modern_server):
    """A tool-level failure is a complete result, not a transport error."""
    result = CliRunner().invoke(main, [modern_server, "tools/call", "nope", "--no-auth"])

    # TOOL_ERROR is the load-bearing half: an unknown tool is a complete MCP
    # result carrying isError, not a protocol failure. A transport error whose
    # message happened to contain the tool name would satisfy the output check
    # below, and it would not report TOOL_ERROR.
    assert result.exit_code == 1, result.output
    assert '"error": "TOOL_ERROR"' in result.output
    assert "Unknown tool" in result.output or "nope" in result.output
