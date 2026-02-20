"""Tests for the CLI module."""

import json
import pytest
import subprocess
import time
import sys
import requests
from pathlib import Path
from click.testing import CliRunner

# Python 3.10 compatibility: ExceptionGroup was added in 3.11
try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup

from murl.cli import (
    main,
    parse_url,
    parse_data_value,
    parse_data_flags,
    map_virtual_path_to_method,
    parse_headers,
)
from murl import __version__


# Test server configuration
TEST_SERVER_PORT = 8765
TEST_SERVER_URL = f"http://localhost:{TEST_SERVER_PORT}"


def parse_ndjson(output):
    """Parse NDJSON (one JSON object per line) output."""
    return [json.loads(line) for line in output.strip().split('\n') if line.strip()]


@pytest.fixture(scope="module")
def mcp_server():
    """Start the real MCP test server for integration tests."""
    test_dir = Path(__file__).parent
    server_script = test_dir / "mcp_test_server.py"

    process = subprocess.Popen(
        [sys.executable, str(server_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    import requests
    max_retries = 10
    retry_delay = 0.2

    for attempt in range(max_retries):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"Server failed to start:\nSTDOUT: {stdout}\nSTDERR: {stderr}")

        try:
            response = requests.post(
                TEST_SERVER_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                timeout=1
            )
            if response.status_code == 200:
                break
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(retry_delay)
    else:
        process.terminate()
        pytest.fail(f"Server failed to start after {max_retries} attempts")

    yield TEST_SERVER_URL

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# Test helper functions

def test_parse_url_tools():
    base, path = parse_url("http://localhost:3000/tools")
    assert base == "http://localhost:3000"
    assert path == "/tools"


def test_parse_url_tools_with_name():
    base, path = parse_url("http://localhost:3000/tools/weather")
    assert base == "http://localhost:3000"
    assert path == "/tools/weather"


def test_parse_url_resources():
    base, path = parse_url("https://api.example.com/mcp/resources")
    assert base == "https://api.example.com/mcp"
    assert path == "/resources"


def test_parse_url_prompts():
    base, path = parse_url("http://localhost:3000/prompts/greeting")
    assert base == "http://localhost:3000"
    assert path == "/prompts/greeting"


def test_parse_url_invalid():
    with pytest.raises(ValueError, match="Invalid MCP URL"):
        parse_url("http://localhost:3000/invalid")


def test_parse_data_value_boolean_true():
    assert parse_data_value("true") is True
    assert parse_data_value("True") is True


def test_parse_data_value_boolean_false():
    assert parse_data_value("false") is False
    assert parse_data_value("False") is False


def test_parse_data_value_integer():
    assert parse_data_value("123") == 123
    assert parse_data_value("-456") == -456


def test_parse_data_value_float():
    assert parse_data_value("3.14") == 3.14
    assert parse_data_value("-2.5") == -2.5


def test_parse_data_value_string():
    assert parse_data_value("hello") == "hello"
    assert parse_data_value("world123") == "world123"


def test_parse_data_flags_key_value():
    result = parse_data_flags(("name=John", "age=30", "active=true"))
    assert result == {"name": "John", "age": 30, "active": True}


def test_parse_data_flags_json():
    result = parse_data_flags(('{"city": "Paris", "metric": true}',))
    assert result == {"city": "Paris", "metric": True}


def test_parse_data_flags_json_array_error():
    with pytest.raises(ValueError, match="JSON arrays are not supported"):
        parse_data_flags(('[1, 2, 3]',))


def test_parse_data_flags_mixed():
    result = parse_data_flags(("name=Alice", '{"age": 25}'))
    assert result == {"name": "Alice", "age": 25}


def test_parse_data_flags_invalid_format():
    with pytest.raises(ValueError, match="Invalid data format"):
        parse_data_flags(("invalid",))


def test_parse_data_flags_invalid_json():
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_data_flags(('{"invalid": json}',))


def test_map_tools_list():
    method, params = map_virtual_path_to_method("/tools", {})
    assert method == "tools/list"
    assert params == {}


def test_map_tools_call():
    data = {"message": "hello"}
    method, params = map_virtual_path_to_method("/tools/echo", data)
    assert method == "tools/call"
    assert params == {"name": "echo", "arguments": {"message": "hello"}}


def test_map_resources_list():
    method, params = map_virtual_path_to_method("/resources", {})
    assert method == "resources/list"
    assert params == {}


def test_map_resources_read():
    method, params = map_virtual_path_to_method("/resources/path/to/file", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file"}


def test_map_resources_read_with_additional_params():
    data = {"format": "json", "encoding": "utf-8"}
    method, params = map_virtual_path_to_method("/resources/path/to/file", data)
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file", "format": "json", "encoding": "utf-8"}


def test_map_resources_read_empty_path():
    with pytest.raises(ValueError, match="path cannot be empty"):
        map_virtual_path_to_method("/resources/", {})


def test_map_resources_read_with_special_characters():
    method, params = map_virtual_path_to_method("/resources/path/to/my%20file.txt", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/my%20file.txt"}


def test_map_resources_read_with_multiple_slashes():
    method, params = map_virtual_path_to_method("/resources/path//to///file", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path//to///file"}


def test_map_resources_read_relative_path():
    method, params = map_virtual_path_to_method("/resources/relative/path", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///relative/path"}


def test_map_prompts_list():
    method, params = map_virtual_path_to_method("/prompts", {})
    assert method == "prompts/list"
    assert params == {}


def test_map_prompts_get():
    data = {"variable": "value"}
    method, params = map_virtual_path_to_method("/prompts/greeting", data)
    assert method == "prompts/get"
    assert params == {"name": "greeting", "arguments": {"variable": "value"}}


def test_parse_headers():
    headers = parse_headers(("Authorization: Bearer token123", "X-Custom: value"))
    assert headers == {
        "Authorization": "Bearer token123",
        "X-Custom": "value"
    }


def test_parse_headers_invalid():
    with pytest.raises(ValueError, match="Invalid header format"):
        parse_headers(("InvalidHeader",))


# Integration tests with real MCP server
# Default output: compact NDJSON (one JSON object per line)

def test_cli_list_tools(mcp_server):
    """Test listing tools outputs NDJSON by default."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/tools", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "echo"
    assert output[1]["name"] == "weather"


def test_cli_call_tool_with_data(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", "message=hello",
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert output[0]["text"] == "hello"


def test_cli_call_weather_tool(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/weather",
        "-d", "city=Paris",
        "-d", "metric=true",
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert "Paris" in output[0]["text"]


def test_cli_list_resources(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/resources", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2
    assert output[0]["uri"] == "file:///path/to/file1.txt"


def test_cli_read_resource(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/resources/test.txt", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["uri"] == "file:///test.txt"
    assert output[0]["text"] == "Mock file content"


def test_cli_list_prompts(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/prompts", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "greeting"


def test_cli_get_prompt(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/prompts/greeting",
        "-d", "name=Alice",
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["role"] == "user"
    assert "Alice" in output[0]["content"]["text"]


def test_cli_with_headers(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/prompts",
        "-H", "Authorization: Bearer token123"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2


def test_cli_verbose_mode(mcp_server):
    """Test -v outputs pretty-printed JSON and debug info."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/tools", "-v", "--no-auth"])

    assert result.exit_code == 0
    # Verbose mixes debug info (stderr) and pretty JSON (stdout) in CliRunner
    assert "=== MCP Request ===" in result.output or len(result.output) > 0
    # Output should contain indentation (pretty-printed)
    assert '  ' in result.output


def test_cli_json_data(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", '{"message": "complex json"}',
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert output[0]["text"] == "complex json"


# Error tests — all errors are structured JSON by default

def test_cli_connection_error():
    """Test connection error outputs structured JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:9999/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "CONNECTION_REFUSED"
    assert "Connection refused" in error_obj["message"]


def test_cli_dns_resolution_error():
    """Test DNS error outputs structured JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["https://invalid-server.test/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "DNS_RESOLUTION_FAILED"
    assert "DNS resolution failed" in error_obj["message"]


def test_cli_timeout_error():
    """Test timeout error outputs structured JSON."""
    from unittest.mock import patch

    runner = CliRunner()

    with patch("murl.cli.make_mcp_request") as mock_request:
        timeout_exc = TimeoutError("Request timed out")
        mock_request.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [timeout_exc])

        result = runner.invoke(main, ["http://localhost:8765/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "TIMEOUT"
    assert "timeout" in error_obj["message"].lower()


def test_cli_generic_connect_error():
    """Test generic ConnectError outputs structured JSON."""
    from unittest.mock import patch

    runner = CliRunner()

    with patch("murl.cli.make_mcp_request") as mock_request:
        class ConnectError(Exception):
            pass

        connect_exc = ConnectError("Some other network error")
        mock_request.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [connect_exc])

        result = runner.invoke(main, ["http://localhost:8765/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert "Some other network error" in error_obj["message"]


def test_cli_invalid_url():
    """Test invalid URL outputs structured JSON error."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/invalid"])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "INVALID_ARGUMENT"
    assert "Invalid MCP URL" in error_obj["message"]


# Flag tests

def test_version_option():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help():
    """Test --help shows concise help."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "USAGE:" in result.output
    assert "EXAMPLES:" in result.output
    assert "AUTHENTICATION:" in result.output
    assert "--login" in result.output
    assert "--no-auth" in result.output


def test_upgrade_option():
    from unittest.mock import patch, MagicMock

    runner = CliRunner()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Successfully installed mcp-curl-0.2.1"
    mock_result.stderr = ""

    with patch('subprocess.run', return_value=mock_result) as mock_run:
        result = runner.invoke(main, ["--upgrade"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == [sys.executable, "-m", "pip", "install", "--upgrade", "mcp-curl"]
        assert call_args[1]['timeout'] == 300

    assert result.exit_code == 0
    assert "Upgrading murl" in result.output
    assert "Upgrade complete" in result.output


# Default output format tests

def test_default_list_output_is_ndjson(mcp_server):
    """Default output for lists is compact NDJSON (one JSON object per line)."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "--no-auth"])

    assert result.exit_code == 0
    lines = result.output.strip().split('\n')
    assert len(lines) > 0

    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)
        # Compact: no whitespace after separators
        assert ', ' not in line and '": ' not in line


def test_default_single_output_is_compact(mcp_server):
    """Default output for single results is compact JSON."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools/echo", "-d", "message=test", "--no-auth"])

    assert result.exit_code == 0
    assert '  ' not in result.output  # No indentation
    lines = result.output.strip().split('\n')
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)


def test_default_error_is_structured_json():
    """Default error output is structured JSON on stderr."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/invalid"])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert "error" in error_obj
    assert "message" in error_obj
    assert "code" in error_obj
    assert error_obj["code"] == 2


def test_default_connection_error_is_structured():
    """Default connection error is structured JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:19999/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert "error" in error_obj
    assert error_obj["error"] in ["CONNECTION_REFUSED", "CONNECTION_ERROR"]


def test_default_missing_url_is_structured():
    """Missing URL produces structured JSON error."""
    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "MISSING_ARGUMENT"
    assert "URL argument is required" in error_obj["message"]


def test_verbose_output_is_pretty_printed(mcp_server):
    """Verbose mode outputs pretty-printed JSON (with indentation)."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "-v", "--no-auth"])

    assert result.exit_code == 0
    assert '  ' in result.output  # Has indentation
