"""How murl reports a tools/call the server answered with a failure.

The MCP spec signals a failed tool with `isError: true`, and a third-party
authorization need with URL elicitation: a `-32042` error (2025-11-25) or an
`InputRequiredResult` (2026-07-28). A successful result is content, except for
the exact top-level auth values in `detect_tool_auth_failure`. Every case runs
through the real CLI against the local test servers.
"""

import json

import pytest
from click.testing import CliRunner

from murl.cli import main

try:
    ExceptionGroup
except NameError:  # Python 3.10
    from exceptiongroup import ExceptionGroup

from .conftest import stderr_runner


def _run(server, tool, *args):
    result = stderr_runner().invoke(main, [server, "tools/call", tool, *args, "--no-auth"])
    lines = [ln for ln in result.stderr.splitlines() if ln.startswith("{")]
    error = json.loads(lines[-1]) if lines else None
    return result, error


def test_is_error_prints_content_and_exits_tool_error(legacy_server):
    result, error = _run(legacy_server, "fail", "-d", "message=Google API returned 401 Unauthorized")
    assert result.exit_code == 1
    assert error["error"] == "TOOL_ERROR"
    # The tool's explanation is on stdout, as on success.
    assert json.loads(result.stdout.strip()) == {
        "type": "text", "text": "Google API returned 401 Unauthorized"}
    # A downstream failure is never answered with murl's own login.
    assert "--login" not in result.stderr


def test_is_error_on_a_later_page_stops_pagination(legacy_server):
    result, error = _run(legacy_server, "fail_page2")
    assert result.exit_code == 1
    assert error["error"] == "TOOL_ERROR"
    assert "at page 2" in error["message"]
    assert "quota exceeded" in result.stdout


def test_raw_returns_an_is_error_result_verbatim(legacy_server):
    result, error = _run(legacy_server, "fail", "--raw")
    assert result.exit_code == 0, result.output
    assert error is None
    assert json.loads(result.stdout.strip()) == {"type": "text", "text": "tool failed"}


def test_url_elicitation_error_exits_authorization_required(legacy_server):
    result, error = _run(legacy_server, "consent")
    assert result.exit_code == 1
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert error["elicitations"] == [
        {"url": "https://auth.example.com/connect?r=1", "host": "auth.example.com",
         "message": "Authorize Google"}]
    assert "https://auth.example.com/connect?r=1" in error["message"]
    # Server prose is kept apart from the vetted URLs.
    assert error["server_message"] == "Authorize Google to continue"
    assert "Authorize Google to continue" not in error["message"]
    assert "--login" not in error["suggestion"]
    assert result.stdout == ""


def test_url_elicitation_error_ignores_raw(legacy_server):
    # -32042 is a request-level error: --raw has no result to return.
    result, error = _run(legacy_server, "consent", "--raw")
    assert result.exit_code == 1
    assert error["error"] == "AUTHORIZATION_REQUIRED"


def test_input_required_url_exits_authorization_required(modern_server):
    result, error = _run(modern_server, "input_required")
    assert result.exit_code == 1
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert error["elicitations"] == [
        {"url": "https://auth.example.com/connect?r=2", "host": "auth.example.com",
         "message": "Authorize Google"}]


def test_input_required_form_exits_input_required(modern_server):
    result, error = _run(modern_server, "input_required", "-d", "kind=form")
    assert result.exit_code == 1
    assert error["error"] == "INPUT_REQUIRED"
    assert error["requests"] == ["elicitation/create"]


def test_input_required_url_and_form_exits_input_required(modern_server):
    # A form request murl cannot answer outranks the URL beside it.
    result, error = _run(modern_server, "input_required", "-d", "kind=mixed")
    assert result.exit_code == 1
    assert error["error"] == "INPUT_REQUIRED"


def test_url_elicitations_skip_entries_without_a_string_url():
    from murl.cli import _url_elicitations
    assert _url_elicitations([
        {"url": "https://a.example/x", "message": "A"},
        {"url": None, "message": "no url"},
        {"url": 7},
        {"message": "missing"},
        "not a dict",
        {"url": "https://b.example/y", "message": 3},
    ]) == [{"url": "https://a.example/x", "host": "a.example", "message": "A"},
           {"url": "https://b.example/y", "host": "b.example", "message": ""}]
    assert _url_elicitations(None) == []


@pytest.mark.parametrize("body", [
    # A server's inbound-bearer rejection.
    {"error": "unauthorized", "reason": "verify-failed", "tool": "gmail_get_message"},
    # A Google API failure returned as a result.
    {"error": "Request had invalid authentication credentials.", "status": 401},
])
def test_auth_value_in_a_successful_result_exits_auth_failed(legacy_server, body):
    result, error = _run(legacy_server, "echo", "-d", f"message={json.dumps(body)}")
    assert result.exit_code == 1
    assert error["error"] == "AUTH_FAILED"
    # The match is a heuristic, so the result is still printed.
    assert json.loads(result.stdout.strip()) == body
    # The cause can be murl's token or the server's third-party account.
    assert "--login" in error["suggestion"] and "third-party" in error["suggestion"]


@pytest.mark.parametrize("body", [
    {"items": [{"text": "User: the API said 401 Unauthorized when I tried"}]},
    {"path": "auth.py", "code": "raise Unauthorized()"},
    {"message": "Unauthorized"},
])
def test_auth_word_in_content_succeeds(legacy_server, body):
    result, error = _run(legacy_server, "echo", "-d", f"message={json.dumps(body)}")
    assert result.exit_code == 0, result.output
    assert error is None
    assert json.loads(result.stdout.strip()) == body


@pytest.mark.parametrize("tool,args,expected", [
    ("consent", [], "AUTHORIZATION_REQUIRED"),
    ("fail", ["-d", "message=401 Unauthorized"], "TOOL_ERROR"),
])
def test_tool_outcome_never_starts_murls_oauth(legacy_server, monkeypatch, tool, args, expected):
    # Without --no-auth, an error whose text says "Unauthorized" reaches the
    # transport 401 handler. A tool outcome must pass through it untouched.
    import murl.cli as cli

    def _no_oauth(*a, **k):
        raise AssertionError("OAuth must not start for a tool outcome")

    monkeypatch.setattr(cli, "authorize", _no_oauth)
    monkeypatch.setattr(cli, "_probe_www_authenticate", _no_oauth)
    result = stderr_runner().invoke(main, [legacy_server, "tools/call", tool, *args])
    lines = [ln for ln in result.stderr.splitlines() if ln.startswith("{")]
    assert result.exit_code == 1, result.output
    assert json.loads(lines[-1])["error"] == expected


def test_tool_error_message_carries_the_tool_text(legacy_server):
    # A caller that reads only stderr still sees why the tool failed.
    result, error = _run(legacy_server, "fail", "-d", "message=quota exceeded",
                         "-d", "second=retry after 60s")
    assert error["error"] == "TOOL_ERROR"
    assert "quota exceeded retry after 60s" in error["message"]
    assert [json.loads(ln)["text"] for ln in result.stdout.strip().splitlines()] == [
        "quota exceeded", "retry after 60s"]


def test_tool_error_text_is_bounded():
    from murl.cli import _tool_error_text
    assert _tool_error_text({"type": "text", "text": "x" * 600}) == "x" * 500 + "..."
    assert _tool_error_text({"error": "e"}) == '{"error":"e"}'


def test_tool_error_with_verbose_prints_content(legacy_server):
    result, error = _run(legacy_server, "fail", "-v")
    assert error["error"] == "TOOL_ERROR"
    assert '"text": "tool failed"' in result.stdout


def test_tool_error_under_toon_without_python_toon_falls_back_to_json(legacy_server, monkeypatch):
    import murl.cli as cli
    monkeypatch.setattr(cli, "toon_encode", None)
    result, error = _run(legacy_server, "fail", "--format", "toon")
    assert error["error"] == "TOOL_ERROR"
    assert json.loads(result.stdout.strip()) == {"type": "text", "text": "tool failed"}


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "ftp://x.example/a",
])
def test_non_http_elicitation_url_is_withheld(legacy_server, url):
    result, error = _run(legacy_server, "consent", "-d", f"url={url}")
    assert error["server_message"] == "Authorize Google to continue"
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert error["elicitations"] == []
    assert url not in result.stderr


def test_url_elicitation_error_without_data(legacy_server):
    result, error = _run(legacy_server, "consent", "-d", "nodata=1")
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert error["elicitations"] == []
    assert "no usable URL" in error["message"]
    assert "Authorize the account with the server another way" in error["suggestion"]


def test_elicitation_message_is_bounded():
    from murl.cli import _url_elicitations
    [el] = _url_elicitations([{"url": "https://a.example", "message": "m" * 1000}])
    assert el["message"] == "m" * 300


def test_input_required_non_http_url_is_withheld(modern_server):
    result, error = _run(modern_server, "input_required", "-d", "kind=badscheme")
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert error["elicitations"] == []
    assert "javascript:" not in result.stderr


@pytest.mark.parametrize("kind", ["url", "form"])
def test_input_required_ignores_raw(modern_server, kind):
    # An InputRequiredResult carries no tool result for --raw to return.
    result, error = _run(modern_server, "input_required", "-d", f"kind={kind}", "--raw")
    assert result.exit_code == 1
    assert error["error"] == ("AUTHORIZATION_REQUIRED" if kind == "url" else "INPUT_REQUIRED")


@pytest.mark.parametrize("kind,requests", [
    ("mixed", ["elicitation/create"]),
    ("sampling", ["elicitation/create", "sampling/createMessage"]),
])
def test_url_beside_other_input_exits_input_required(modern_server, kind, requests):
    result, error = _run(modern_server, "input_required", "-d", f"kind={kind}")
    assert error["error"] == "INPUT_REQUIRED"
    assert error["requests"] == requests


def test_outcome_on_a_later_page_names_the_page():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mcp.shared.exceptions import MCPError
    from mcp.types import InputRequiredResult
    from murl.cli import AuthorizationRequired, InputRequired, _follow_tool_pagination

    first = [{"type": "text", "text": json.dumps({"items": ["a"], "hasMore": True, "nextCursor": "c"})}]
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=MCPError(-32042, "Authorize", {"elicitations": [
        {"mode": "url", "url": "https://a.example/c", "message": "A"}]}))
    with pytest.raises(AuthorizationRequired) as auth:
        asyncio.run(_follow_tool_pagination(session, "t", {}, first, 1000, False))
    assert auth.value.page == 2
    assert auth.value.elicitations == [{"url": "https://a.example/c", "host": "a.example", "message": "A"}]

    session.call_tool = AsyncMock(return_value=InputRequiredResult.model_validate({
        "resultType": "input_required", "inputRequests": {"r": {
            "method": "elicitation/create",
            "params": {"mode": "form", "message": "x", "requestedSchema": {"type": "object", "properties": {}}}}}}))
    with pytest.raises(InputRequired) as inp:
        asyncio.run(_follow_tool_pagination(session, "t", {}, first, 1000, False))
    assert inp.value.page == 2


def test_outcome_text_never_triggers_the_stale_session_retry(monkeypatch):
    # A server message such as "Session expired, authorize to continue" is the
    # tool's answer, not a dead session: run once, keep the cached session.
    import asyncio
    import murl.cli as cli
    from murl import session_store

    url = "http://localhost:1/mcp"
    session_store.save_session(url, "sess-live")
    calls = []

    async def _fake(*a, **k):
        calls.append(1)
        raise cli.AuthorizationRequired("Session expired, authorize to continue", [])

    monkeypatch.setattr(cli, "_run_mcp_session", _fake)
    with pytest.raises(cli.AuthorizationRequired):
        asyncio.run(cli.make_mcp_request(url, "tools/call", {"name": "t"}, {}, False,
                                         session_reuse=True))
    assert len(calls) == 1
    assert session_store.get_session_id(url) == "sess-live"


@pytest.mark.parametrize("url", [
    "http:evil",
    "https:///x",
    " https://a.example",
    "https://a\nb.example",
    "https://a.example/x\x7f",
    "https://accounts.google.com@evil.example/x",
    "https://evil.example\\.accounts.google.com/x",
    "https://\u0430ccounts.google.com/x",
    "https://xn--ccounts-ezh.google.com\u2028/x",
    "https://a.example/\u202egnp.exe",
    "https://a.example/\u200bx",
    "https://[bad",
    "https://a.example/" + "x" * 2048,
])
def test_misleading_elicitation_urls_are_withheld(url):
    from murl.cli import _url_elicitations
    assert _url_elicitations([{"url": url, "message": "m"}]) == []


@pytest.mark.parametrize("url,host", [
    ("https://a.example/x", "a.example"),
    ("http://127.0.0.1:8080/cb", "127.0.0.1"),
    ("https://[::1]:8443/cb", "::1"),
    ("https://xn--ccounts-ezh.google.com/x", "xn--ccounts-ezh.google.com"),
])
def test_plain_hosts_are_kept(url, host):
    from murl.cli import _url_elicitations
    assert _url_elicitations([{"url": url}])[0]["host"] == host


def test_url_elicitations_keep_url_mode_and_cap_the_count():
    from murl.cli import _url_elicitations
    form = {"mode": "form", "url": "https://a.example/f", "message": "form"}
    many = [{"url": f"https://a.example/{i}"} for i in range(15)]
    kept = _url_elicitations([form, {"mode": "url", "url": "https://a.example/u"}] + many)
    assert [e["url"] for e in kept][:2] == ["https://a.example/u", "https://a.example/0"]
    assert len(kept) == 10


def test_auth_failed_detail_is_bounded():
    from murl.cli import detect_tool_auth_failure
    hit = detect_tool_auth_failure({"error": "unauthorized", "message": "m" * 5000})
    assert hit == "unauthorized: " + "m" * 300


def test_an_unknown_tool_outcome_is_not_reported_as_auth_failed(capsys):
    from murl.cli import ToolOutcome, _report_tool_outcome
    with pytest.raises(SystemExit):
        _report_tool_outcome(ToolOutcome("something new"), False, "json")
    error = json.loads(capsys.readouterr().err.strip())
    assert error["error"] == "TOOL_ERROR"
    assert "--login" not in json.dumps(error)


def test_url_elicitation_in_a_nested_exception_group_through_the_cli(monkeypatch):
    # anyio nests task groups, so a -32042 can arrive two groups deep.
    import murl.cli as cli
    outcome = cli.AuthorizationRequired("Authorize Google", [
        {"url": "https://auth.example.com/c", "host": "auth.example.com", "message": "A"}])

    async def _nested(*a, **k):
        raise ExceptionGroup("outer", [ExceptionGroup("inner", [outcome])])

    monkeypatch.setattr(cli, "_run_mcp_session", _nested)
    result = stderr_runner().invoke(main, ["http://localhost:1/mcp", "tools/call", "t", "--no-auth"])
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert result.exit_code == 1
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert error["elicitations"][0]["url"] == "https://auth.example.com/c"


def test_input_required_result_with_no_requests_is_input_required():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mcp.types import InputRequiredResult
    from murl.cli import InputRequired, _call_tool
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=InputRequiredResult.model_validate(
        {"resultType": "input_required", "requestState": "s1"}))
    with pytest.raises(InputRequired) as e:
        asyncio.run(_call_tool(session, "t", {}, False))
    assert e.value.methods == []


def test_auth_failed_mid_pagination_keeps_earlier_pages():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from murl.cli import ToolAuthError, _follow_tool_pagination
    page2 = MagicMock()
    block = MagicMock()
    block.model_dump.return_value = {"type": "text", "text": json.dumps({"status": 403, "body": "x"})}
    page2.content = [block]
    page2.is_error = False
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=page2)
    first = [{"type": "text", "text": json.dumps({"items": ["a", "b"], "hasMore": True, "nextCursor": "c"})}]
    with pytest.raises(ToolAuthError) as e:
        asyncio.run(_follow_tool_pagination(session, "t", {}, first, 1000, False))
    assert e.value.page == 2
    assert e.value.content == ["a", "b", {"status": 403, "body": "x"}]


def test_input_required_url_has_no_server_message(modern_server):
    result, error = _run(modern_server, "input_required")
    assert error["error"] == "AUTHORIZATION_REQUIRED"
    assert "server_message" not in error
    assert error["elicitations"][0]["message"] == "Authorize Google"


def test_input_required_with_no_requests_says_so():
    from murl.cli import InputRequired
    assert str(InputRequired([])) == "Server requires input murl cannot provide: (none given)"


def test_input_required_methods_are_bounded():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from murl.cli import InputRequired, _call_tool

    def _req(method):
        r = MagicMock()
        r.method = method
        r.params = MagicMock(mode="form")
        return r

    result = MagicMock(spec=["input_requests"])
    result.input_requests = {str(i): _req(f"m{i:02d}" + "x" * 200) for i in range(15)}
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=result)
    import murl.cli as cli
    orig = cli.InputRequiredResult
    cli.InputRequiredResult = MagicMock
    try:
        with pytest.raises(InputRequired) as e:
            asyncio.run(_call_tool(session, "t", {}, False))
    finally:
        cli.InputRequiredResult = orig
    assert len(e.value.methods) == 10
    assert all(len(m) == 100 for m in e.value.methods)
