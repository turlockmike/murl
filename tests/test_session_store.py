"""Unit tests for murl.session_store (the Mcp-Session-Id cache).

SESSIONS_DIR is redirected to a per-test tmp dir by the autouse fixture in
conftest.py, so these exercise the real filesystem backend in isolation.
"""

import json
import os
import time

from murl import session_store


URL = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations"


def _record_path(url):
    return session_store.SESSIONS_DIR / f"{session_store._key_for_url(url)}.json"


def test_roundtrip():
    session_store.save_session(URL, "sess-abc", protocol_version="2025-11-25", identity="idA")
    assert session_store.get_session_id(URL, "idA") == "sess-abc"
    rec = session_store._load_record(URL)
    assert rec["protocol_version"] == "2025-11-25"
    assert rec["server_url"] == URL
    assert rec["identity"] == "idA"


def test_get_missing_returns_none():
    assert session_store.get_session_id("https://nope.example/mcp") is None


def test_clear_removes_record():
    session_store.save_session(URL, "sess-abc")
    assert session_store.get_session_id(URL) is not None
    assert session_store.clear_session(URL) is True
    assert session_store.get_session_id(URL) is None


def test_clear_nonexistent_is_ok():
    # Clearing a URL with no cached session must not raise and reports success.
    assert session_store.clear_session("https://never-saved.example/mcp") is True


def test_distinct_urls_are_isolated():
    session_store.save_session("https://a.example/mcp", "sess-a")
    session_store.save_session("https://b.example/mcp", "sess-b")
    assert session_store.get_session_id("https://a.example/mcp") == "sess-a"
    assert session_store.get_session_id("https://b.example/mcp") == "sess-b"


def test_identity_mismatch_is_not_replayed():
    # Cross-identity protection: a session saved under one credential must never
    # be replayed under another (even one that still authenticates).
    session_store.save_session(URL, "sess-idA", identity="idA")
    assert session_store.get_session_id(URL, "idA") == "sess-idA"
    assert session_store.get_session_id(URL, "idB") is None
    assert session_store.get_session_id(URL, "") is None


def test_unauthenticated_identity_roundtrips():
    # --no-auth callers share the empty-string identity.
    session_store.save_session(URL, "sess-anon")  # identity defaults to ""
    assert session_store.get_session_id(URL) == "sess-anon"
    assert session_store.get_session_id(URL, "") == "sess-anon"


def test_expired_record_is_dropped():
    session_store.save_session(URL, "sess-old")
    path = _record_path(URL)
    record = json.loads(path.read_text())
    record["updated_at"] = time.time() - (session_store.DEFAULT_SESSION_TTL_SECONDS + 60)
    path.write_text(json.dumps(record))

    assert session_store.get_session_id(URL) is None
    # Stale entries are cleaned up as a side effect of the failed lookup.
    assert not path.exists()


def test_ttl_zero_disables_reuse(monkeypatch):
    monkeypatch.setenv("MURL_SESSION_TTL", "0")
    session_store.save_session(URL, "sess-x")
    # TTL 0 hard-disables reuse even when saved and read in the same tick.
    assert session_store.get_session_id(URL) is None


def test_ttl_env_override_extends_window(monkeypatch):
    # A record just past the default window is still fresh under a larger TTL.
    monkeypatch.setenv("MURL_SESSION_TTL", str(session_store.DEFAULT_SESSION_TTL_SECONDS + 10_000))
    session_store.save_session(URL, "sess-x")
    path = _record_path(URL)
    record = json.loads(path.read_text())
    record["updated_at"] = time.time() - (session_store.DEFAULT_SESSION_TTL_SECONDS + 60)
    path.write_text(json.dumps(record))
    assert session_store.get_session_id(URL) == "sess-x"


def test_invalid_ttl_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MURL_SESSION_TTL", "not-a-number")
    assert session_store._ttl_seconds() == session_store.DEFAULT_SESSION_TTL_SECONDS


def test_save_refreshes_timestamp():
    session_store.save_session(URL, "sess-x")
    first = json.loads(_record_path(URL).read_text())["updated_at"]
    time.sleep(0.01)
    session_store.save_session(URL, "sess-x")
    second = json.loads(_record_path(URL).read_text())["updated_at"]
    assert second > first


def test_corrupt_record_returns_none():
    session_store.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _record_path(URL).write_text("{ not valid json")
    assert session_store.get_session_id(URL) is None


def test_record_without_session_id_returns_none():
    session_store.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _record_path(URL).write_text(json.dumps({"updated_at": time.time()}))
    assert session_store.get_session_id(URL) is None


def test_saved_record_is_owner_only_readable():
    session_store.save_session(URL, "sess-x")
    assert (_record_path(URL).stat().st_mode & 0o777) == 0o600


def test_rewrite_resets_broadened_permissions():
    # The atomic mkstemp+replace write always lands a fresh 0600 file, so even
    # rewriting over a pre-existing world-readable file ends up 0600.
    session_store.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _record_path(URL)
    path.write_text("{}")
    os.chmod(path, 0o644)
    session_store.save_session(URL, "sess-x")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_save_leaves_no_temp_litter():
    # The atomic write must clean up after itself — no .tmp- files left behind.
    session_store.save_session(URL, "sess-x")
    leftovers = [p.name for p in session_store.SESSIONS_DIR.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_sessions_dir_is_owner_only():
    # Created 0700 so other users can't enumerate which URLs have live sessions.
    session_store.save_session(URL, "sess-x")
    assert (session_store.SESSIONS_DIR.stat().st_mode & 0o777) == 0o700
