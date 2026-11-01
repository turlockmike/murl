"""Shared pytest fixtures.

Two of these start real local MCP servers, one per protocol revision, so any
test can drive murl against either without setup boilerplate:

    def test_something(legacy_server):   # 2025 era: initialize handshake
        runner.invoke(main, [legacy_server, "tools/list", "--no-auth"])

    def test_something_else(modern_server):   # 2026-07-28: no handshake
        runner.invoke(main, [modern_server, "tools/list", "--no-auth"])

Both yield a base URL and shut down afterwards. To poke at them by hand instead,
run `python tests/serve_test_server.py both`.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
import requests

# Ports are fixture-local so a hand-started `serve_test_server.py` (8765/8767)
# can run alongside a test session without colliding.
LEGACY_SERVER_PORT = 8768
MODERN_SERVER_PORT = 8769
#: A 2025-era server whose errors a proxy has rewritten.
PROXIED_SERVER_PORT = 8770

#: The `_meta` envelope every 2026-07-28 request must carry. Exported for tests
#: that build raw JSON-RPC bodies rather than going through murl.
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _serve(script: str, port: int, probe: dict, probe_headers: dict | None = None):
    """Spawn a test server, wait for it to answer, yield its URL, then stop it."""
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / script)],
        env={"TEST_PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://localhost:{port}/mcp"
    try:
        for _ in range(25):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"{script} failed to start:\nSTDOUT: {stdout}\nSTDERR: {stderr}")
            try:
                # Any answer proves the socket is up — including a protocol
                # rejection, which is what the strict 2026 server returns to a
                # probe it considers malformed.
                requests.post(url, json=probe, headers=probe_headers or {}, timeout=1)
                break
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(0.2)
        else:
            pytest.fail(f"{script} never became reachable on port {port}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture(scope="module")
def legacy_server():
    """A 2025-era MCP server: `initialize` handshake, plain JSON results."""
    yield from _serve(
        "mcp_test_server.py",
        LEGACY_SERVER_PORT,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


@pytest.fixture(scope="module")
def proxied_legacy_server():
    """A 2025-era server behind a proxy that rewrites its JSON-RPC errors.

    Models Amazon Bedrock AgentCore, which collapses a
    runtime-side error into a generic 400 — so the `-32601` this server sends for
    `server/discover` reaches the client as `-32010 "Received error (400) from
    runtime"` instead. A client that reads the protocol era off the error code
    cannot reach these servers at all.

    Everything else about the server is unchanged, so `initialize` still
    succeeds: what this fixture tests is whether the fallback runs.
    """
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "mcp_test_server.py")],
        env={"TEST_PORT": str(PROXIED_SERVER_PORT), "MCP_TEST_PROXY_REWRITE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://localhost:{PROXIED_SERVER_PORT}/mcp"
    try:
        for _ in range(25):
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"proxied server failed to start:\nSTDOUT: {stdout}\nSTDERR: {stderr}")
            try:
                requests.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=1
                )
                break
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(0.2)
        else:
            pytest.fail(f"proxied server never became reachable on port {PROXIED_SERVER_PORT}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture(scope="module")
def modern_server():
    """A 2026-07-28 MCP server. Strict on purpose — it REFUSES `initialize`.

    Requires the `_meta` envelope, `Mcp-Method`, and a matching `Mcp-Name` on
    named methods, and stamps `resultType`/`ttlMs`/`cacheScope` on results. Its
    expectations were captured from the official `mcp` 2.0.0 client's wire
    output, so a request shaped differently from the reference client's fails.
    """
    yield from _serve(
        "mcp_test_server_2026.py",
        MODERN_SERVER_PORT,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}},
        {"Mcp-Method": "tools/list"},
    )


def stderr_runner() -> CliRunner:
    """A CliRunner whose result keeps stderr apart from stdout. Click 8.2+
    always does; 8.0-8.1 needs mix_stderr=False (pyproject allows >=8.0)."""
    if "mix_stderr" in CliRunner.__init__.__code__.co_varnames:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def ndjson(output: str) -> list[dict]:
    """Parse murl's NDJSON stdout into a list of objects."""
    return [json.loads(line) for line in output.strip().split("\n") if line.strip()]


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    """Point the MCP session cache at a per-test tmp dir.

    Session reuse is on by default, so without this any test that hits a server
    which issues an Mcp-Session-Id (e.g. the public-server integration tests)
    would read/write the real ~/.murl/sessions. Redirecting it keeps tests
    hermetic and prevents cross-test bleed.
    """
    monkeypatch.setattr("murl.session_store.SESSIONS_DIR", tmp_path / "murl-sessions")
