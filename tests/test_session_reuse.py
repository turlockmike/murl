"""End-to-end tests for MCP session reuse against a session-aware test server.

These prove the two fixes for the AgentCore session explosion:

  1. A single invocation that paginates a tools/call uses ONE MCP session
     (one initialize) for all pages — not one session per page.
  2. Consecutive invocations replay the cached Mcp-Session-Id and stay on ONE
     server session instead of minting a new one each time.

plus the opt-outs (--no-session / MURL_NO_SESSION) and the stale-session
fallback. The server records one JSON line per lifecycle event so we can assert
exactly how many sessions were minted vs reused.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from click.testing import CliRunner

from murl import session_store
from murl.cli import main

from .conftest import stderr_runner


def _free_port() -> int:
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_events(state_file: Path):
    if not state_file.exists():
        return []
    return [json.loads(line) for line in state_file.read_text().splitlines() if line.strip()]


def _initializes(state_file: Path):
    return [e for e in _read_events(state_file) if e["event"] == "initialize"]


@pytest.fixture
def session_server(tmp_path):
    """A session-aware test server (subprocess) with a per-test event log."""
    state_file = tmp_path / "events.ndjson"
    port = _free_port()
    server_script = Path(__file__).parent / "mcp_test_server.py"

    import os
    env = {
        **os.environ,
        "TEST_PORT": str(port),
        "MCP_TEST_SESSIONS": "1",
        "MCP_TEST_STATE_FILE": str(state_file),
        "MCP_TEST_PAGES": "3",
    }
    process = subprocess.Popen(
        [sys.executable, str(server_script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_url = f"http://localhost:{port}"
    for _ in range(10):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"Session server failed to start:\nSTDOUT: {stdout}\nSTDERR: {stderr}")
        try:
            resp = requests.post(
                base_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                timeout=1,
            )
            if resp.status_code == 200:
                break
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(0.2)
    else:
        process.terminate()
        pytest.fail("Session server failed to start")

    yield SimpleNamespace(url=base_url, state_file=state_file)

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def test_paginated_tool_call_uses_a_single_session(session_server):
    """A paginated tools/call must run every page on one session (one
    initialize) rather than one session — and one AgentCore microVM — per page.
    """
    runner = CliRunner()
    result = runner.invoke(
        main, [f"{session_server.url}/mcp", "tools/call", "paginate", "--no-auth"]
    )
    assert result.exit_code == 0, result.output

    items = [json.loads(line) for line in result.output.strip().split("\n") if line.strip()]
    assert items == [f"item-{i}" for i in range(6)]  # 3 pages × 2 items, aggregated

    inits = _initializes(session_server.state_file)
    assert len(inits) == 1, f"expected ONE session for all pages, saw: {_read_events(session_server.state_file)}"


def test_session_is_reused_across_invocations(session_server):
    """Two separate invocations should replay the cached Mcp-Session-Id and stay
    on the same server session instead of minting a second one."""
    runner = CliRunner()
    url = f"{session_server.url}/mcp"

    r1 = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth"])
    r2 = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth"])
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output

    sid1 = json.loads(r1.output.strip())["session_id"]
    sid2 = json.loads(r2.output.strip())["session_id"]
    assert sid1 == sid2 == "sess-1"

    inits = _initializes(session_server.state_file)
    assert len(inits) == 2
    minted = [e for e in inits if e["minted"]]
    assert len(minted) == 1, f"second call should reuse, not mint: {inits}"
    assert all(e["session_id"] == "sess-1" for e in inits)

    # The negotiated session is persisted for the next invocation.
    assert session_store.get_session_id(url) is not None


def test_no_session_flag_disables_reuse(session_server):
    """--no-session negotiates a fresh session each call and persists nothing."""
    runner = CliRunner()
    url = f"{session_server.url}/mcp"

    r1 = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth", "--no-session"])
    r2 = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth", "--no-session"])
    assert r1.exit_code == 0 and r2.exit_code == 0

    inits = _initializes(session_server.state_file)
    assert len(inits) == 2
    assert all(e["minted"] for e in inits), f"each --no-session call should mint fresh: {inits}"
    assert len({e["session_id"] for e in inits}) == 2, "sessions should differ"
    assert session_store.get_session_id(url) is None, "nothing should be persisted with --no-session"


def test_murl_no_session_env_disables_reuse(session_server, monkeypatch):
    """MURL_NO_SESSION is an env-level equivalent of --no-session."""
    monkeypatch.setenv("MURL_NO_SESSION", "1")
    runner = CliRunner()
    url = f"{session_server.url}/mcp"

    r1 = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth"])
    r2 = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth"])
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output

    inits = _initializes(session_server.state_file)
    assert len(inits) == 2
    assert len({e["session_id"] for e in inits}) == 2
    assert session_store.get_session_id(url) is None


def test_stale_cached_session_is_discarded_and_renegotiated(session_server):
    """A cached session the server no longer recognizes (404 / 'Session
    terminated') is dropped and a fresh one negotiated transparently."""
    url = f"{session_server.url}/mcp"
    # Pre-seed a session the server never minted; its initialize will 404.
    session_store.save_session(url, "stale-deadbeef")

    runner = CliRunner()
    result = runner.invoke(main, [url, "tools/call", "session_info", "--no-auth"])
    assert result.exit_code == 0, result.output

    # The retry negotiated a fresh, server-minted session.
    assert json.loads(result.output.strip())["session_id"] == "sess-1"

    assert session_store.get_session_id(url) == "sess-1"

    events = _read_events(session_server.state_file)
    assert any(e["event"] == "initialize_rejected" for e in events), events
    assert any(e["event"] == "initialize" and e["minted"] for e in events), events


@pytest.mark.parametrize("tool,expected", [("fail", "TOOL_ERROR"), ("consent", "AUTHORIZATION_REQUIRED")])
def test_tool_outcome_after_a_stale_session_retry(session_server, tool, expected):
    """The renegotiated call reports a tool outcome the same way as a first
    call, not as an exception group."""
    url = f"{session_server.url}/mcp"
    session_store.save_session(url, "stale-deadbeef")

    result = stderr_runner().invoke(main, [url, "tools/call", tool, "--no-auth"])
    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert error["error"] == expected
    if tool == "consent":
        assert error["elicitations"][0]["url"] == "https://auth.example.com/connect?r=1"
    events = _read_events(session_server.state_file)
    assert any(e["event"] == "initialize_rejected" for e in events), events


@pytest.mark.parametrize("tool", ["fail", "consent"])
def test_session_is_cached_after_a_tool_outcome(session_server, tool):
    """The server answered on the session, so it is reused next time."""
    url = f"{session_server.url}/mcp"
    result = CliRunner().invoke(main, [url, "tools/call", tool, "--no-auth"])
    assert result.exit_code == 1, result.output
    assert session_store.get_session_id(url) == "sess-1"
