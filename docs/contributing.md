# Contributing

## Development Setup

```bash
git clone https://github.com/turlockmike/murl.git
cd murl
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest
pytest --cov=murl --cov-report=html
```

No network, credentials, or cloud access needed — the integration tests start
local MCP servers themselves.

### Local MCP servers, one per protocol revision

murl has to speak two MCP protocol revisions, because the servers it calls
upgrade at different times. `tests/` ships one server for each:

| Revision | Script | Default port | Shape |
| --- | --- | --- | --- |
| 2025 era | `mcp_test_server.py` | 8765 | `initialize` handshake, plain JSON results |
| 2026-07-28 | `mcp_test_server_2026.py` | 8767 | no handshake — `server/discover`, `_meta` envelope, `Mcp-Method`/`Mcp-Name` headers, `resultType` |

Both expose the same two tools (`echo`, `weather`), so the only thing that
differs between them is the revision.

**To drive them by hand:**

```bash
python tests/serve_test_server.py both     # or: 2025 / 2026, plus --port
```

It prints one NDJSON line per server (parseable) and the probe commands
(readable), then serves until Ctrl-C:

```bash
murl http://localhost:8765/mcp tools/list --no-auth   # 2025 era
murl http://localhost:8767/mcp tools/list --no-auth   # 2026-07-28
```

**To use them in a test**, request a fixture — no setup code needed:

```python
def test_my_feature(legacy_server):        # or modern_server
    result = CliRunner().invoke(main, [legacy_server, "tools/list", "--no-auth"])
    assert result.exit_code == 0
```

Both fixtures live in `tests/conftest.py`, yield a base URL, and clean up after
themselves. `conftest.py` also exports `MODERN_META` (the `_meta` envelope a
2026-07-28 request must carry, for tests that build raw JSON-RPC bodies) and
`ndjson()` (parses murl's stdout).

The 2026 server is strict on purpose: it refuses `initialize` outright, and
rejects a missing envelope key (`-32602`), a missing or mismatched `Mcp-Method`
(`-32020`), and a missing or wrong `Mcp-Name` on methods that name a target.
A permissive stub cannot prove a client correct, so its expectations were
captured from the official `mcp` Python client's real wire output rather than
read off the spec — if murl sends something the reference client would not, the
test fails.

One gotcha if you extend these servers: use `ThreadingHTTPServer`, not
`HTTPServer`. These handlers speak HTTP/1.1, and a single-threaded server plus
keep-alive deadlocks on the second request of a reused connection (it looks like
a client read-timeout).

### How murl picks a revision

`_negotiate_era` in `murl/cli.py` probes `server/discover` first and falls back
to the `initialize` handshake. The SDK leaves that policy to the caller —
`discover()` propagates every error except a version mismatch, including the
`METHOD_NOT_FOUND` a 2025-era server returns for a method it does not have.

Discover-first, not handshake-first: a 2026-07-28 server refuses `initialize`
outright, so leading with the handshake fails against exactly the servers the new
revision exists for. A 2025-era server answers the probe with a recoverable
error, so this ordering costs one extra round trip against old servers and works
against both — the reverse only works against old ones.

Session reuse is 2025-era only, because the newer revision has no protocol
sessions. The transport no longer exposes the negotiated id, so murl reads
`Mcp-Session-Id` off the response headers with an httpx event hook; a
sessionless server simply never sets it.

## How It Works

murl translates REST-like URLs into MCP JSON-RPC 2.0 requests:

1. **Parses the URL** — extracts the base endpoint and virtual path
2. **Maps the path** — converts to the appropriate MCP method (`/tools` → `tools/list`, `/tools/echo` → `tools/call`)
3. **Parses `-d` flags** — builds method parameters with automatic type coercion
4. **Sends JSON-RPC** — HTTP POST to the base endpoint
5. **Returns the result** — extracts and prints the JSON-RPC response

## Releasing

Update the version in `pyproject.toml` and `murl/__init__.py`, then tag the merge commit:

```bash
git tag v0.X.Y
git push origin v0.X.Y
```

This triggers a GitHub Actions workflow that:
1. Builds and publishes to PyPI
2. Creates a GitHub release with artifacts
3. Updates the [Homebrew tap](https://github.com/turlockmike/homebrew-murl) formula automatically
