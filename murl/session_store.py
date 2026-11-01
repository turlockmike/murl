"""Persistent storage for MCP Streamable HTTP session IDs.

MCP's Streamable HTTP transport assigns a session via the ``Mcp-Session-Id``
header returned on the ``initialize`` response; clients are expected to replay
that header on every subsequent request so the server can keep them attached to
the same session (MCP 2025-11-25 §Session Management).

murl is a one-shot CLI, so without persistence every invocation — and, before
the single-session pagination fix, every page of a paginated ``tools/call`` —
negotiated a *brand new* session. On Amazon Bedrock AgentCore that is acute:
the runtime provisions a fresh microVM for every request that arrives without a
known session ID, and always stamps an ``Mcp-Session-Id`` on the response even
for stateless servers. A script that called murl in a loop could spin up
a runtime session per call.

This module caches the negotiated session ID per server URL so consecutive
invocations replay it and stay pinned to the same runtime session instead of
churning new ones. Two safety properties matter:

- **Identity binding.** A session is server-side routing/context, and AgentCore
  stamps one even for stateless servers. A cached ID is therefore only replayed
  under the *same* credential identity it was negotiated with (see ``identity``
  below): if the bearer token is rotated to a different identity — even one that
  still authenticates, so no 401 fires — the cached ID is treated as a miss and
  a fresh session is negotiated. This prevents a request from silently landing
  in a prior identity's server-side context on a shared/CI host.
- **Freshness.** Entries are only reused within a TTL (``MURL_SESSION_TTL``
  seconds, default 300); a stale or server-rejected ID is discarded and a fresh
  session negotiated transparently (see ``murl.cli._is_session_terminated_error``).

Session IDs are routing tokens, not secrets — authorization is governed by the
bearer token on each request — but they are written with restrictive
permissions out of caution. Storage is filesystem-only:
``~/.murl/sessions/<sha256(url)>.json``, written atomically.
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path.home() / ".murl" / "sessions"

# Default reuse window. AgentCore runtime sessions are reaped after an idle
# period (commonly ~15 min) and capped at a max lifetime, so a conservative
# default keeps us from replaying an ID that is almost certainly dead while
# still covering tight script loops. The stale-session fallback in the CLI is
# the actual correctness guarantee; this TTL is just an optimization to skip a
# doomed attempt. Override with MURL_SESSION_TTL (seconds); <= 0 disables reuse.
DEFAULT_SESSION_TTL_SECONDS = 300
_TTL_ENV = "MURL_SESSION_TTL"


def _key_for_url(server_url: str) -> str:
    """Return a SHA-256 hash of the server URL for use as a storage key."""
    return hashlib.sha256(server_url.encode()).hexdigest()


def _ttl_seconds() -> int:
    """Resolve the session reuse window from MURL_SESSION_TTL (seconds).

    Falls back to the default on an unset or unparseable value. A value <= 0
    disables reuse by treating every cached session as already stale.
    """
    raw = os.environ.get(_TTL_ENV)
    if raw is None:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS


def _path_for_key(key: str) -> Path:
    return SESSIONS_DIR / f"{key}.json"


def _load_record(server_url: str) -> Optional[dict]:
    """Read and parse the raw cache record for a server URL, or None.

    Internal: returns the on-disk row as-is (no TTL/identity checks). Returns
    None when the file is absent, unreadable, corrupt, or not a JSON object.
    """
    path = _path_for_key(_key_for_url(server_url))
    if not path.exists():
        return None
    try:
        with open(path) as f:
            record = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return record if isinstance(record, dict) else None


def get_session_id(server_url: str, identity: str = "") -> Optional[str]:
    """Return the cached session ID to replay for a server URL, or None.

    Returns None — i.e. negotiate a fresh session — when there is no usable
    record, the record was negotiated under a different credential ``identity``
    (see module docstring), or it is older than the TTL. A stale record is
    deleted as a side effect so the cache does not accumulate dead entries; an
    identity mismatch is left in place (the next save for this URL overwrites it).

    ``identity`` is an opaque, stable per-credential fingerprint supplied by the
    caller (``murl.cli._identity_fingerprint`` hashes the bearer token); the
    empty string denotes an unauthenticated caller.
    """
    record = _load_record(server_url)
    if record is None or not record.get("session_id"):
        return None

    # Identity binding: never replay a session negotiated under a different
    # credential. Don't clear it — a concurrent caller with the original
    # identity may still be using it; the next save for this URL overwrites it.
    if record.get("identity", "") != identity:
        return None

    updated_at = record.get("updated_at")
    ttl = _ttl_seconds()
    # `>=` so MURL_SESSION_TTL=0 hard-disables reuse even when a record is saved
    # and read within the same clock tick (age == 0.0).
    if not isinstance(updated_at, (int, float)) or (time.time() - updated_at) >= ttl:
        clear_session(server_url)
        return None
    return record["session_id"]


def save_session(
    server_url: str,
    session_id: str,
    protocol_version: Optional[str] = None,
    identity: str = "",
) -> None:
    """Persist (or refresh) the session record for a server URL.

    Refreshing updates the timestamp so an actively-used session keeps sliding
    its TTL forward and is not reaped mid-script. The write is atomic — a temp
    file in the same directory plus ``os.replace`` — so two murl processes
    hitting the same URL (the loop-in-a-script case this targets) can't
    interleave a half-written JSON. ``identity`` binds the entry to the
    credential it was negotiated with.
    """
    # mode=0o700 at creation (0o700 has no group/other bits for the umask to
    # leave open), so the dir is never briefly world-traversable — otherwise
    # another user could enumerate the sha256 filenames (which URLs have live
    # sessions). exist_ok keeps an already-correct dir untouched.
    os.makedirs(SESSIONS_DIR, mode=0o700, exist_ok=True)

    record = {
        "session_id": session_id,
        "protocol_version": protocol_version,
        "identity": identity,
        "server_url": server_url,
        "updated_at": time.time(),
    }
    path = _path_for_key(_key_for_url(server_url))
    # Atomic write: mkstemp creates a 0600 temp in the same filesystem, and
    # os.replace renames it over the target atomically (POSIX & Windows). The
    # final file therefore inherits 0600 on every write, new or rewritten.
    fd, tmp = tempfile.mkstemp(dir=str(SESSIONS_DIR), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        # Don't leave a temp behind if the write/replace failed.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_session(server_url: str) -> bool:
    """Delete the cached session for a server URL. Returns True on success."""
    path = _path_for_key(_key_for_url(server_url))
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
