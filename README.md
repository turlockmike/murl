# murl - MCP Curl

[![Tests](https://github.com/turlockmike/murl/actions/workflows/test.yml/badge.svg)](https://github.com/turlockmike/murl/actions/workflows/test.yml)

A curl-like CLI for [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers. Query tools, resources, and prompts using simple REST-like URLs.

**LLM-friendly:** compact JSON output, NDJSON streaming, structured errors to stderr, semantic exit codes. Built for agents to call from shell.

<p align="center">
  <img src="images/logo.png" alt="murl logo" width="400">
</p>

## Installation

### Homebrew (Recommended)

```bash
brew install turlockmike/murl/murl
```

### pip

Requires Python 3.10+.

```bash
pip install mcp-curl
# with OS keychain token storage and TOON output
pip install "mcp-curl[keychain,toon]"
```

On a Homebrew Python, a bare `pip install` is refused with
`error: externally-managed-environment` ([PEP 668](https://peps.python.org/pep-0668/)).
Use `pipx install mcp-curl`, or install into a dedicated venv.

### Shell script

```bash
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/master/install.sh | bash
```

To upgrade: `brew upgrade turlockmike/murl/murl` or `murl --upgrade`

## Quick Start

```bash
# List tools on a server (NDJSON output — one JSON object per line)
murl https://mcp.deepwiki.com/mcp/tools | jq -r '.name'

# Call a tool — response is auto-unwrapped from the MCP text envelope
murl https://remote.mcpservers.org/fetch/mcp/tools/fetch -d url=https://example.com

# Paginated tools auto-follow nextCursor and emit NDJSON
murl https://your-server/mcp/tools/list_things -d filter=active > all.ndjson
```

**Public demo servers:**
- `https://mcp.deepwiki.com/mcp` — GitHub repository docs
- `https://remote.mcpservers.org/fetch/mcp` — fetch web content

## Usage

```bash
murl <url> [options]
```

### URL Mapping

| URL Path | MCP Method |
|---|---|
| `/tools` | `tools/list` |
| `/tools/<name>` | `tools/call` |
| `/resources` | `resources/list` |
| `/resources/<path>` | `resources/read` |
| `/prompts` | `prompts/list` |
| `/prompts/<name>` | `prompts/get` |

### Default Behavior

`tools/call` responses are post-processed by default so the common case
needs zero flags:

1. **Auto-unwrap the text envelope.** MCP tool responses are returned as
   `[{type:"text", text:"<stringified JSON>"}]`. murl detects this shape
   and emits the inner JSON directly.
2. **Auto-follow `nextCursor`.** When the unwrapped body carries
   `{hasMore: true, nextCursor}`, murl follows the cursor and emits the
   aggregated items as NDJSON to stdout. Capped at `--max-pages` (default
   1000) with cursor-cycle protection.
3. **Report a failed call as an error.** Each case exits 1 with a
   structured error on stderr:

   | The server sends | `error` | stdout |
   |---|---|---|
   | A result with `isError: true` | `TOOL_ERROR` | The tool's content, as on success |
   | JSON-RPC error `-32042`, or an `InputRequiredResult` of URL elicitations (third-party authorization) | `AUTHORIZATION_REQUIRED`, with `elicitations: [{url, host, message}]`; a `-32042` also carries the server's text in `server_message` | Empty |
   | Another `InputRequiredResult` (form, sampling, roots) | `INPUT_REQUIRED`, with `requests` (at most 10 method names of 100 characters) | Empty |
   | A successful result that reports an auth failure (below) | `AUTH_FAILED` | The result, as on success |

   For `AUTHORIZATION_REQUIRED`, open the URL, complete the authorization,
   then re-run the command. murl's own token is unchanged, so `--login` does
   not help. The server writes the URL, so murl passes on at most 10, and
   only `http`/`https` URLs whose host is a plain ASCII hostname or IP
   address, with no userinfo (`user@`), no whitespace or control
   characters, and at most 2048 characters. `host` is
   the parsed host to show the user. Messages are cut to 300 characters.

   `TOOL_ERROR` repeats the tool's text (up to 500 characters) in its
   message. During pagination, a `TOOL_ERROR` on page N stops the walk and
   prints only that page. An `AUTH_FAILED` on page N prints the earlier
   pages' items and then page N.

   Exit 0 means the server answered with a result. It does not prove the
   tool reached its downstream service: a server can report a failure as
   ordinary text, which murl prints and exits 0.

   A successful result reports an auth failure only through a whole value
   at its top level: `error`, `error.code`, `code` or `status` equal to
   `unauthorized`, `unauthenticated`, `invalid_token`, `token_expired`,
   `auth_failed` or `authentication_failed` (any case); or an integer
   `status`/`statusCode` of 401 or 403. Plain text matches when it is only
   one of those codes or a status line such as `401 Unauthorized`. Nested
   fields, list items and words inside longer text are content.

Use `--raw` to disable all three and return the protocol response verbatim. A `-32042` error or an `InputRequiredResult` still exits 1, since it carries no result to return.

### Examples

```bash
# List tools
murl http://localhost:3000/tools

# Call a tool (response is auto-unwrapped to inner JSON)
murl http://localhost:3000/tools/echo -d message=hello

# Numbers, booleans, and JSON: use key:=value for a raw JSON value
murl http://localhost:3000/tools/search -d q=widgets -d limit:=5 -d active:=true

# Paginated tool — get every page as NDJSON, no manual cursor loop
murl http://localhost:3000/tools/list_things -d filter=active > all.ndjson

# Cap pagination
murl http://localhost:3000/tools/list_things --max-pages 5

# Multiple arguments (auto type-coerced)
murl http://localhost:3000/tools/weather -d city=Paris -d metric=true

# JSON data
murl http://localhost:3000/tools/config -d '{"settings": {"theme": "dark"}}'

# Custom headers
murl http://localhost:3000/tools -H "Authorization: Bearer token123"

# Raw protocol response (no unwrap, no cursor follow)
murl http://localhost:3000/tools/echo -d message=hi --raw

# Verbose mode (pretty-prints output, shows request debug info)
murl http://localhost:3000/tools -v
```

### Options

| Flag | Description |
|---|---|
| `-d, --data` | Add `key=value` (string), `key:=value` (raw JSON — number/bool/null/object/array), inline JSON, `@file`, or `@-` (stdin). Repeatable. |
| `-H, --header` | Add HTTP header. Repeatable. |
| `-v, --verbose` | Pretty-print output, show request debug info. |
| `--raw` | Skip auto-unwrap and cursor follow; emit the protocol response verbatim. |
| `--max-pages <N>` | Cap auto-pagination on `tools/call` responses (default 1000). |
| `--format toon` | Output in [TOON](https://github.com/toon-format/spec) format. |
| `--login` | Force OAuth re-authentication. |
| `--no-auth` | Skip all authentication. |
| `--client-id <id>` | Pre-registered OAuth client ID (skips dynamic registration). |
| `--client-secret <s>` | Pre-registered OAuth client secret (or `MURL_CLIENT_SECRET`). |
| `--callback-port <n>` | Fixed port for the OAuth callback redirect URI. |
| `--scope <scopes>` | OAuth scope to request. |
| `--no-session` | Don't reuse/persist the MCP session — negotiate a fresh `Mcp-Session-Id` each call (or set `MURL_NO_SESSION`). |
| `--no-mcp-config` | Skip `.mcp.json` discovery for OAuth client defaults. |
| `--version` | Show version info. |
| `--upgrade` | Upgrade to latest version. |

### TOON Output

Use `--format toon` for [TOON](https://github.com/toon-format/spec) output, which uses fewer tokens than JSON when feeding results into LLM contexts. Requires the optional `python-toon` package:

```bash
pip install "mcp-curl[toon]"

# List tools in TOON format — produces fewer tokens than JSON for structured data
murl https://mcp.deepwiki.com/mcp/tools --format toon
```

### OAuth

murl supports OAuth 2.0 with Dynamic Client Registration (RFC 7591) and PKCE. Tokens are cached automatically.

```bash
# First call triggers browser-based OAuth flow
murl https://example.com/mcp/tools

# Skip auth for public servers
murl https://example.com/mcp/tools --no-auth

# Force re-authentication
murl https://example.com/mcp/tools --login

# Pre-registered OAuth client (skip Dynamic Client Registration)
murl --client-id 0oax4xo --callback-port 8080 https://example.com/mcp/tools
```

### `.mcp.json` Discovery

murl walks up from the current directory looking for a sibling `.mcp.json`
and uses it to fill in OAuth defaults — same convention Claude Code uses.
When the requested base URL exactly matches an entry's `url` field (after
URL decoding and trailing-slash normalization), murl picks up:

- `oauth.clientId` → fills `--client-id` if not passed
- `oauth.callbackPort` → fills `--callback-port` if not passed

```json
{
  "mcpServers": {
    "myserver": {
      "type": "http",
      "url": "https://example.com/mcp",
      "oauth": {
        "clientId": "0oax4xo",
        "callbackPort": 8080
      }
    }
  }
}
```

Matching is **exact** (not prefix): a caller URL with extra path segments
or a different query string is treated as a different runtime and falls
through to dynamic client registration. Use `--no-mcp-config` to disable
discovery entirely.

### Session Reuse

MCP's Streamable HTTP transport assigns a session via the `Mcp-Session-Id`
header on the `initialize` response; clients are expected to replay it on
subsequent requests ([MCP 2025-11-25 §Session
Management](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#session-management)).
murl captures that ID and reuses it — both across the pages of a paginated
`tools/call` and across separate invocations (cached per server URL in
`~/.murl/sessions/`).

This matters most on **Amazon Bedrock AgentCore**, which provisions a fresh
microVM for any request that arrives with a new or absent session ID and
stamps an `Mcp-Session-Id` on every response even for stateless servers.
Without reuse, a script that calls murl in a loop spins up a new runtime
session — and microVM — per call. Reuse keeps the whole script pinned to one.

```bash
# A loop like this reuses one session instead of opening one per call
for id in $(cat ids.txt); do
  murl "$SERVER/mcp" tools/call get_item -d id="$id"
done
```

- **On by default.** Disable with `--no-session` or `MURL_NO_SESSION=1` (e.g.
  for a strict stateful server you don't want sharing state across calls).
- **Reuse window:** a cached session is replayed for `MURL_SESSION_TTL` seconds
  (default `300`) since last use. Past that it's renegotiated.
- **Self-healing:** if the server rejects a cached session (HTTP 404 /
  "Session terminated", and similar 400/410 signals), murl discards it and
  negotiates a fresh one transparently. `murl --login` also starts a fresh
  session.
- **Identity-scoped:** a cached session is only replayed under the same bearer
  token it was negotiated with, so a rotated or different credential never
  reuses another identity's session.
- Sessions are routing tokens, not credentials (authorization is governed by
  the bearer token on each request), but the per-URL cache is written
  atomically with `0600` files under a `0700` directory regardless.

## Documentation

- [Output & Exit Codes](docs/agent-mode.md) — NDJSON format, structured errors, exit codes
- [MCP Server Setup](docs/mcp-servers.md) — mcp-proxy, Streamable HTTP, local servers
- [Contributing](docs/contributing.md) — development setup, testing, releasing
- [CHANGELOG](CHANGELOG.md) — release history and breaking changes

## Requirements

- Python 3.10+

## License

MIT
