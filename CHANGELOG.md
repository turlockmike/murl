# Changelog

## Unreleased

### Changed

- **A tool result with `isError: true` exits 1 with `TOOL_ERROR`.** The
  tool's content still goes to stdout. A page with `isError: true` stops
  pagination with the same error. `--raw` returns the result verbatim and
  exits 0.
- **`make_mcp_request` raises a `ToolOutcome` for a failed `tools/call`.**
  In-process callers get `ToolCallError` (isError; the content is
  `.content`), `AuthorizationRequired` (`.elicitations`) or `InputRequired`
  (`.methods`) where the call returned content or raised an
  `ExceptionGroup`. Catch `ToolCallError` to keep reading a tool error as a
  result.

### Added

- **`AUTHORIZATION_REQUIRED` for third-party authorization.** A `-32042`
  error or an `InputRequiredResult` of URL elicitations exits 1 with each
  `http`/`https` URL and its message in `elicitations`. Any other
  `InputRequiredResult` exits 1 with `INPUT_REQUIRED`.

### Fixed

- **Tool-level `AUTH_FAILED` ignores auth words in content.** A successful
  tool result whose content quoted "Unauthorized" (a search hit, a
  transcript, source code) exited `AUTH_FAILED`. Only a whole top-level
  value matches: `error`, `error.code`, `code` or `status` equal to an auth
  code, an integer `status`/`statusCode` of 401 or 403, or plain text that
  is only a code or a status line. The suggestion names both causes:
  murl's token (`--login`) or the server's third-party account.

## 0.7.0

### Added

- **MCP session reuse.** murl now captures the `Mcp-Session-Id` a Streamable
  HTTP server assigns at `initialize` and replays it on later requests — both
  within a single invocation and *across* invocations (cached per server URL
  under `~/.murl/sessions/`). Previously every invocation negotiated a
  brand-new session. On Amazon Bedrock AgentCore — where a request with a
  new/absent session ID provisions a whole microVM, and the platform stamps an
  `Mcp-Session-Id` on every response even for stateless servers — a script
  calling murl in a loop could spin up a runtime session per call. Reuse keeps those
  calls pinned to one runtime session. On by default; opt out with
  `--no-session` or `MURL_NO_SESSION`. The reuse window is `MURL_SESSION_TTL`
  seconds (default 300). A `--login` re-auth (and a 401 re-auth) starts a fresh
  session, and a cached session the server rejects (404 / "Session terminated"
  and similar 400/410 signals, per MCP 2025-11-25 §Session Management) is
  discarded and renegotiated transparently. Reuse is bound to the credential
  identity (a hash of the bearer token) so a rotated or different token never
  replays another identity's session, and the per-URL cache is written
  atomically (`mkstemp` + `os.replace`, `0600`) under a `0700` directory.
  Because reused sessions are left open for the next caller, murl no longer
  sends a session-terminating `DELETE` on every exit.
- **Both MCP protocol revisions.** murl reaches 2026-07-28 servers as well as
  2025-era ones. It probes `server/discover` first and falls back to the
  `initialize` handshake. Session reuse stays 2025-era only, since the newer
  revision has no protocol sessions. A local test server is added for each
  revision (`python tests/serve_test_server.py both`).

### Changed

- **Requires `mcp>=2.0.0` and declares `httpx` directly.** Without this, a
  fresh install had no `httpx`, and `murl --version` raised
  `ModuleNotFoundError`.
- **TOON output follows TOON spec v4.4.** `python-toon` is now
  `>=0.2.0,<0.3.0`. Empty arrays print as `[]` instead of `[0]:`, and tabular
  headers drop the trailing delimiter (`[2]{a}:`). This changes the output of
  `--format toon`.

### Fixed

- **AgentCore-hosted servers are reachable again.** A proxy rewrote the
  `server/discover` error code, so every call failed with `EXCEPTIONGROUP`
  before `initialize`. Any probe failure now falls back to the handshake, and
  the probe's error is kept if the handshake also fails.
- **OAuth starts when a first-time 401 arrives without a status code.** The
  401 was nested in `ExceptionGroup`s with no status code, so murl exited with
  `EXCEPTIONGROUP`. When the request carried no credentials, murl now probes
  `WWW-Authenticate` once and starts OAuth.
- **`--login` accepts an authorization server on another host.** This applies
  when the Protected Resource Metadata names the requested resource and every
  hop is on the MCP server's host over https (RFC 9728 §3.3).
- **Auto-paginated `tools/call` no longer opens a new MCP session per page.**
  The cursor-follow loop ran each page through a separate `make_mcp_request` —
  its own transport, `initialize`, and session — so a tool returning N pages
  cost N sessions (and, on AgentCore, N microVMs). All pages of a paginated
  `tools/call` now run on one session, matching how list pagination already
  worked. Unwrap, `--max-pages`, cursor-cycle protection, the
  `hasMore`-without-`nextCursor` warning, and mid-pagination auth-failure
  detection are unchanged.

## 0.6.0

### Added

- **Method-faithful CLI surface.** The second positional argument is now the
  literal MCP JSON-RPC method: `murl <url> tools/call -d name=echo`,
  `murl <url> tools/list`, `murl <url> resources/read -d uri=...`
  (or `resources/read <uri>`), `murl <url> prompts/get -d name=...`,
  `murl <url> resources/templates/list`. Validated against the client-issuable
  method set; server→client methods (`sampling/createMessage`, `roots/list`,
  `elicitation/create`) and notifications are rejected with a clear error.
- **`--allow-unknown-method`** sends a method not in murl's known set, for
  forward-compatibility with version-gated/new spec methods.
- The legacy URL-as-method form (`murl <url>/tools/echo`) still works but is
  **deprecated** — it emits a structured `DEPRECATED_SURFACE` warning to stderr
  under `-v`, and is slated for removal at 1.0.

### Fixed

- **Crash with `ExceptionGroup` after a successful MCP response.** A late
  exception during the SDK's stream/session teardown could escape *after* the
  response was produced, exiting non-zero and discarding a completed result.
  Post-response teardown exceptions are now isolated.
- **`-d key:=value` (raw JSON) now works.** Previously the parser split on
  `=` first, leaving a trailing colon on the key (`{"limit:": 5}` instead of
  `{"limit": 5}`), so the argument was silently dropped and the tool fell
  back to its default. `:=` is now split as a token and the value parsed as
  raw JSON (number/bool/null/object/array). Plain `key=value` strings are
  unchanged.
- **Tool/prompt names containing `/` are no longer truncated.** A name like
  `namespace/subtool` was reduced to its first segment (`namespace`), calling
  the wrong tool. All path segments after the category are now joined, matching
  how `resources/` already worked.
- **List pagination no longer stops on a falsy-but-valid cursor.** End of
  pagination is now detected with `nextCursor is None` rather than a truthiness
  check, so an empty-string cursor doesn't truncate results.
- **`tools/call` responses that report `hasMore` without a cursor now warn**
  on stderr instead of silently returning partial data.
- **Token refresh failures fall back to full re-authentication.** A network
  error or malformed token response during refresh previously crashed with an
  unhandled exception; it now re-authorizes like an `OAuthError` would.
- **Error classification uses the exception type, not message substrings.** A
  tool error whose text merely contains `ValidationError`/`Timeout` is no
  longer misclassified.

### Docs

- Install instructions document the Python 3.10+ prereq and the pipx/venv
  route for Homebrew Pythons (PEP 668).

## 0.5.1

### Fixed

- **`--upgrade` keeps the optional extras.** It installs
  `mcp-curl[keychain,toon]`, so an upgrade does not drop keychain storage
  or TOON output.

## 0.5.0

**Breaking — `tools/call` output shape changed by default.**

Four protocol-level quirks that every shell consumer used to hand-roll
are now handled transparently. All zero-config; `--raw` opts out.

### Added

- **Auto-unwrap** the canonical `[{type:"text", text:"<json>"}]` envelope.
  When a `tools/call` response is a single text block containing valid
  JSON, murl emits the inner JSON directly.
- **Auto-follow `nextCursor`** on `tools/call` responses. When the
  unwrapped body carries `{hasMore: true, nextCursor}`, murl follows the
  cursor and emits aggregated items as NDJSON. Strips `perPage` /
  `pageSize` / `status` on cursor calls (many MCP tools reject the
  combination since the cursor already encodes that context). Cap with
  `--max-pages` (default 1000); cursor-cycle protection built in.
- **`.mcp.json` discovery.** Walks up from CWD, exact-matches the base
  URL against `mcpServers[*].url` (URL-decoded, trailing-slash normalized),
  and fills `--client-id` / `--callback-port` from `oauth.clientId` /
  `oauth.callbackPort`. Mirrors Claude Code's discovery semantics.
- **Tool-level `AUTH_FAILED` detection.** When the response body carries
  `{error: {code: "invalid_token"}}` or an `AUTH_FAILED` marker (HTTP 200
  but the downstream provider rejected the token), murl exits with a
  structured `AUTH_FAILED` error to stderr instead of forcing callers to
  substring-match on stdout.
- **`--raw`** — opt out of auto-unwrap and cursor follow; return the
  protocol response verbatim.
- **`--max-pages <N>`** — cap auto-pagination.
- **`--no-mcp-config`** — skip `.mcp.json` discovery.

### Breaking

- **`tools/call` output is now the unwrapped inner JSON by default.** A
  call that returned `[{"type":"text","text":"{...}"}]` in 0.4.0 now
  returns the parsed `{...}` object. Pass `--raw` to preserve the old
  shape.
- **Paginated `tools/call` responses now emit NDJSON of aggregated
  items.** A call that returned a single envelope with a `nextCursor` in
  0.4.0 now emits one JSON object per line across all pages. Pass
  `--raw` to disable cursor-follow.

### Migration

```bash
# 0.4.0 — manual unwrap + cursor loop
RAW=$(murl "$URL/tools/list_things" -d filter=active)
INNER=$(jq -r '.[0].text' <<<"$RAW")
jq -c '.things[]' <<<"$INNER" > all.ndjson
# ...followed by hand-rolled nextCursor follow loop

# 0.5.0 — same result
murl "$URL/tools/list_things" -d filter=active > all.ndjson

# 0.5.0 — preserve 0.4.0 behavior
murl "$URL/tools/list_things" -d filter=active --raw
```

## 0.4.0

- `--client-id` skips origin check
- Host-level fallback for OAuth metadata discovery
- MCP 2025-11-25 spec compliance + real-world OAuth
- System keychain credential storage with filesystem fallback
- `-d @-` and `-d @file` for Unix pipeline composability
- TOON output format support
