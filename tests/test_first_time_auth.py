"""First-time auth when the transport 401 arrives without a status code.

The MCP SDK surfaces a transport 401 as a status-less
``MCPError("Server returned an error response")`` nested two ExceptionGroups
deep (observed against https://mcp.atlassian.com/v2/mcp with no stored
credentials). murl must still start OAuth rather than report EXCEPTIONGROUP.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

# Python 3.10 compatibility: ExceptionGroup was added in 3.11
try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup

from murl.cli import main

URL = "https://s.test/mcp"
CHALLENGE = 'Bearer resource_metadata="https://s.test/.well-known/oauth-protected-resource/mcp"'


def _nested_group(exc: Exception) -> Exception:
    return ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [exc])],
    )


def _statusless_mcp_error() -> Exception:
    return Exception("Server returned an error response")


def _status_error(code: int, www_authenticate: str) -> httpx.HTTPStatusError:
    response = MagicMock(spec=httpx.Response)
    response.status_code = code
    response.headers = {"www-authenticate": www_authenticate}
    return httpx.HTTPStatusError(
        str(code), request=MagicMock(spec=httpx.Request), response=response,
    )


def _fail_once(exc: Exception, calls: list):
    async def fake_request(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise exc
        return [{"name": "ok"}]
    return fake_request


def _always_fail(exc: Exception):
    async def fake_request(*args, **kwargs):
        raise exc
    return fake_request


def test_statusless_401_without_creds_probes_and_starts_oauth():
    calls: list = []
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate", return_value=CHALLENGE) as mock_probe, \
         patch("murl.cli.authorize", return_value={"access_token": "tok"}) as mock_auth, \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.session_store.clear_session"), \
         patch("murl.cli.make_mcp_request",
               side_effect=_fail_once(_nested_group(_statusless_mcp_error()), calls)):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert mock_probe.called
    assert mock_auth.call_args.kwargs["www_authenticate"] == CHALLENGE
    assert len(calls) == 2
    assert result.exit_code == 0, result.output


def test_401_two_groups_deep_is_detected_without_probe():
    calls: list = []
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize", return_value={"access_token": "tok"}) as mock_auth, \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.session_store.clear_session"), \
         patch("murl.cli.make_mcp_request",
               side_effect=_fail_once(_nested_group(_status_error(401, CHALLENGE)), calls)):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert not mock_probe.called
    assert mock_auth.call_args.kwargs["www_authenticate"] == CHALLENGE
    assert result.exit_code == 0, result.output


def test_403_two_groups_deep_takes_step_up_without_probe():
    challenge = 'Bearer error="insufficient_scope", scope="files:write"'
    calls: list = []
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize", return_value={"access_token": "tok"}) as mock_auth, \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.session_store.clear_session"), \
         patch("murl.cli.make_mcp_request",
               side_effect=_fail_once(_nested_group(_status_error(403, challenge)), calls)):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert not mock_probe.called
    assert mock_auth.call_args.kwargs["scope"] == "files:write"
    assert result.exit_code == 0, result.output


def test_statusless_error_with_auth_header_does_not_probe():
    with patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize") as mock_auth, \
         patch("murl.cli.make_mcp_request",
               side_effect=_always_fail(_nested_group(_statusless_mcp_error()))):
        result = CliRunner().invoke(main, [URL, "tools/list", "-H", "Authorization: Bearer x"])

    assert not mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0


def test_statusless_error_with_no_auth_does_not_probe():
    with patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize") as mock_auth, \
         patch("murl.cli.make_mcp_request",
               side_effect=_always_fail(_nested_group(_statusless_mcp_error()))):
        result = CliRunner().invoke(main, [URL, "tools/list", "--no-auth"])

    assert not mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0


def test_statusless_error_when_probe_sees_no_401_reports_error():
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate", return_value=None) as mock_probe, \
         patch("murl.cli.authorize") as mock_auth, \
         patch("murl.cli.make_mcp_request",
               side_effect=_always_fail(_nested_group(_statusless_mcp_error()))):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0


def test_403_listed_before_401_still_starts_oauth_with_401_challenge():
    """A nested 403 without insufficient_scope must not hide a later 401."""
    group = ExceptionGroup("tg", [
        _status_error(403, 'Bearer error="access_denied"'),
        _status_error(401, CHALLENGE),
    ])
    calls: list = []
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize", return_value={"access_token": "tok"}) as mock_auth, \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.session_store.clear_session"), \
         patch("murl.cli.make_mcp_request", side_effect=_fail_once(group, calls)):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert not mock_probe.called
    assert mock_auth.call_args.kwargs["www_authenticate"] == CHALLENGE
    assert "scope" not in mock_auth.call_args.kwargs
    assert result.exit_code == 0, result.output


def test_403_whose_message_contains_401_takes_step_up_path():
    """The status code picks the branch; a URL port like :8401 in the message does not."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = 403
    response.headers = {"www-authenticate": 'Bearer error="insufficient_scope", scope="files:write"'}
    status_err = httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url 'http://localhost:8401/mcp'",
        request=MagicMock(spec=httpx.Request), response=response,
    )
    calls: list = []
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize", return_value={"access_token": "tok"}) as mock_auth, \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.session_store.clear_session"), \
         patch("murl.cli.make_mcp_request", side_effect=_fail_once(_nested_group(status_err), calls)):
        result = CliRunner().invoke(main, ["http://localhost:8401/mcp", "tools/list"])

    assert not mock_probe.called
    assert mock_auth.call_args.kwargs["scope"] == "files:write"
    assert result.exit_code == 0, result.output


def test_connect_error_whose_message_contains_401_does_not_start_oauth():
    failure = httpx.ConnectError("All connection attempts failed: localhost:8401")
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize") as mock_auth, \
         patch("murl.cli.make_mcp_request", side_effect=_always_fail(_nested_group(failure))):
        result = CliRunner().invoke(main, ["http://localhost:8401/mcp", "tools/list"])

    assert not mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0


def test_statusless_error_mentioning_401_still_starts_oauth():
    calls: list = []
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize", return_value={"access_token": "tok"}) as mock_auth, \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.session_store.clear_session"), \
         patch("murl.cli.make_mcp_request",
               side_effect=_fail_once(_nested_group(Exception("HTTP 401 Unauthorized")), calls)):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert not mock_probe.called
    assert mock_auth.called
    assert result.exit_code == 0, result.output


def test_missing_tool_name_does_not_probe():
    """A local usage error is reported as INVALID_ARGUMENT, never as an auth challenge."""
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize") as mock_auth:
        result = CliRunner().invoke(main, [URL, "tools/call"])

    assert not mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0


def test_direct_request_validation_error_is_invalid_argument():
    """Raised before any connection (no task group), it still reports INVALID_ARGUMENT."""
    import json as _json
    from murl.cli import RequestValidationError

    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.make_mcp_request",
               side_effect=_always_fail(RequestValidationError("Unsupported method: x/y"))):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert not mock_probe.called
    assert result.exit_code == 2
    assert _json.loads(result.output.strip())["error"] == "INVALID_ARGUMENT"


def test_unsupported_capability_does_not_probe():
    """murl's own capability check raised inside the session is not an auth challenge."""
    from murl.cli import RequestValidationError

    failure = _nested_group(RequestValidationError("Server does not support 'prompts'. Supported: tools"))
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize") as mock_auth, \
         patch("murl.cli.make_mcp_request", side_effect=_always_fail(failure)):
        result = CliRunner().invoke(main, [URL, "prompts/list"])

    assert not mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0


@pytest.mark.parametrize("failure", [
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("refused"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
], ids=lambda e: type(e).__name__)
def test_transport_failure_reports_without_probe(failure):
    """A server that did not answer gets no second, unauthenticated request."""
    with patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli._probe_www_authenticate") as mock_probe, \
         patch("murl.cli.authorize") as mock_auth, \
         patch("murl.cli.make_mcp_request", side_effect=_always_fail(_nested_group(failure))):
        result = CliRunner().invoke(main, [URL, "tools/list"])

    assert not mock_probe.called
    assert not mock_auth.called
    assert result.exit_code != 0
