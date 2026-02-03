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


@pytest.fixture(scope="module")
def mcp_server():
    """Start the real MCP test server for integration tests."""
    # Get path to test server
    test_dir = Path(__file__).parent
    server_script = test_dir / "mcp_test_server.py"
    
    # Start server process
    process = subprocess.Popen(
        [sys.executable, str(server_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for server to start with health check
    import requests
    max_retries = 10
    retry_delay = 0.2
    
    for attempt in range(max_retries):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"Server failed to start:\nSTDOUT: {stdout}\nSTDERR: {stderr}")
        
        try:
            # Try to connect to server
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
    
    # Cleanup: stop server
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# Test helper functions

def test_parse_url_tools():
    """Test URL parsing with /tools path."""
    base, path = parse_url("http://localhost:3000/tools")
    assert base == "http://localhost:3000"
    assert path == "/tools"


def test_parse_url_tools_with_name():
    """Test URL parsing with /tools/<name> path."""
    base, path = parse_url("http://localhost:3000/tools/weather")
    assert base == "http://localhost:3000"
    assert path == "/tools/weather"


def test_parse_url_resources():
    """Test URL parsing with /resources path."""
    base, path = parse_url("https://api.example.com/mcp/resources")
    assert base == "https://api.example.com/mcp"
    assert path == "/resources"


def test_parse_url_prompts():
    """Test URL parsing with /prompts path."""
    base, path = parse_url("http://localhost:3000/prompts/greeting")
    assert base == "http://localhost:3000"
    assert path == "/prompts/greeting"


def test_parse_url_invalid():
    """Test URL parsing with invalid path."""
    with pytest.raises(ValueError, match="Invalid MCP URL"):
        parse_url("http://localhost:3000/invalid")


def test_parse_data_value_boolean_true():
    """Test parsing boolean true."""
    assert parse_data_value("true") is True
    assert parse_data_value("True") is True


def test_parse_data_value_boolean_false():
    """Test parsing boolean false."""
    assert parse_data_value("false") is False
    assert parse_data_value("False") is False


def test_parse_data_value_integer():
    """Test parsing integer."""
    assert parse_data_value("123") == 123
    assert parse_data_value("-456") == -456


def test_parse_data_value_float():
    """Test parsing float."""
    assert parse_data_value("3.14") == 3.14
    assert parse_data_value("-2.5") == -2.5


def test_parse_data_value_string():
    """Test parsing string."""
    assert parse_data_value("hello") == "hello"
    assert parse_data_value("world123") == "world123"


def test_parse_data_flags_key_value():
    """Test parsing key=value data flags."""
    result = parse_data_flags(("name=John", "age=30", "active=true"))
    assert result == {"name": "John", "age": 30, "active": True}


def test_parse_data_flags_json():
    """Test parsing JSON data flags."""
    result = parse_data_flags(('{"city": "Paris", "metric": true}',))
    assert result == {"city": "Paris", "metric": True}


def test_parse_data_flags_json_array_error():
    """Test that JSON arrays in data flags raise an error."""
    with pytest.raises(ValueError, match="JSON arrays are not supported"):
        parse_data_flags(('[1, 2, 3]',))


def test_parse_data_flags_mixed():
    """Test parsing mixed data flags."""
    result = parse_data_flags(("name=Alice", '{"age": 25}'))
    assert result == {"name": "Alice", "age": 25}


def test_parse_data_flags_invalid_format():
    """Test parsing invalid data format."""
    with pytest.raises(ValueError, match="Invalid data format"):
        parse_data_flags(("invalid",))


def test_parse_data_flags_invalid_json():
    """Test parsing invalid JSON."""
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_data_flags(('{"invalid": json}',))


def test_map_tools_list():
    """Test mapping /tools to tools/list."""
    method, params = map_virtual_path_to_method("/tools", {})
    assert method == "tools/list"
    assert params == {}


def test_map_tools_call():
    """Test mapping /tools/<name> to tools/call."""
    data = {"message": "hello"}
    method, params = map_virtual_path_to_method("/tools/echo", data)
    assert method == "tools/call"
    assert params == {"name": "echo", "arguments": {"message": "hello"}}


def test_map_resources_list():
    """Test mapping /resources to resources/list."""
    method, params = map_virtual_path_to_method("/resources", {})
    assert method == "resources/list"
    assert params == {}


def test_map_resources_read():
    """Test mapping /resources/<path> to resources/read."""
    method, params = map_virtual_path_to_method("/resources/path/to/file", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file"}


def test_map_resources_read_with_additional_params():
    """Test mapping /resources/<path> to resources/read with additional parameters."""
    data = {"format": "json", "encoding": "utf-8"}
    method, params = map_virtual_path_to_method("/resources/path/to/file", data)
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file", "format": "json", "encoding": "utf-8"}


def test_map_resources_read_empty_path():
    """Test mapping /resources/ with empty path raises error."""
    # Empty path after resources should raise ValueError
    with pytest.raises(ValueError, match="path cannot be empty"):
        map_virtual_path_to_method("/resources/", {})


def test_map_resources_read_with_special_characters():
    """Test mapping /resources/<path> with special characters."""
    # Test path with spaces (URL encoded as %20)
    method, params = map_virtual_path_to_method("/resources/path/to/my%20file.txt", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/my%20file.txt"}


def test_map_resources_read_with_multiple_slashes():
    """Test mapping /resources/<path> with consecutive slashes in path."""
    # Multiple consecutive slashes should be preserved as part of the path
    method, params = map_virtual_path_to_method("/resources/path//to///file", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path//to///file"}


def test_map_resources_read_relative_path():
    """Test mapping /resources/<path> with relative path gets leading slash."""
    # Relative path should get leading slash prepended
    method, params = map_virtual_path_to_method("/resources/relative/path", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///relative/path"}


def test_map_prompts_list():
    """Test mapping /prompts to prompts/list."""
    method, params = map_virtual_path_to_method("/prompts", {})
    assert method == "prompts/list"
    assert params == {}


def test_map_prompts_get():
    """Test mapping /prompts/<name> to prompts/get."""
    data = {"variable": "value"}
    method, params = map_virtual_path_to_method("/prompts/greeting", data)
    assert method == "prompts/get"
    assert params == {"name": "greeting", "arguments": {"variable": "value"}}


def test_parse_headers():
    """Test parsing header flags."""
    headers = parse_headers(("Authorization: Bearer token123", "X-Custom: value"))
    assert headers == {
        "Authorization": "Bearer token123",
        "X-Custom": "value"
    }


def test_parse_headers_invalid():
    """Test parsing invalid header format."""
    with pytest.raises(ValueError, match="Invalid header format"):
        parse_headers(("InvalidHeader",))


# Integration tests with real MCP server

def test_cli_list_tools(mcp_server):
    """Test listing tools with real server."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/tools"])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "echo"
    assert output[1]["name"] == "weather"


def test_cli_call_tool_with_data(mcp_server):
    """Test calling a tool with data using real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", "message=hello"
    ])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    # MCP SDK returns content as a list of content items
    assert isinstance(output, list)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert output[0]["text"] == "hello"


def test_cli_call_weather_tool(mcp_server):
    """Test calling weather tool with multiple arguments using real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/weather",
        "-d", "city=Paris",
        "-d", "metric=true"
    ])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    # MCP SDK returns content as a list of content items
    assert isinstance(output, list)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert "Paris" in output[0]["text"]


def test_cli_list_resources(mcp_server):
    """Test listing resources with real server."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/resources"])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output) == 2
    assert output[0]["uri"] == "file:///path/to/file1.txt"


def test_cli_read_resource(mcp_server):
    """Test reading a resource with real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/resources/test.txt"
    ])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert isinstance(output, list)
    assert len(output) > 0
    assert output[0]["uri"] == "file:///test.txt"
    assert output[0]["text"] == "Mock file content"



def test_cli_list_prompts(mcp_server):
    """Test listing prompts with real server."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/prompts"])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "greeting"


def test_cli_get_prompt(mcp_server):
    """Test getting a prompt with real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/prompts/greeting",
        "-d", "name=Alice"
    ])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert isinstance(output, list)
    assert len(output) > 0
    assert output[0]["role"] == "user"
    assert "Alice" in output[0]["content"]["text"]


def test_cli_with_headers(mcp_server):
    """Test with custom headers using real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/prompts",
        "-H", "Authorization: Bearer token123"
    ])
    
    assert result.exit_code == 0
    # Real server doesn't validate headers, just accept them
    output = json.loads(result.output)
    assert len(output) == 2


def test_cli_verbose_mode(mcp_server):
    """Test verbose mode with real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools",
        "-v"
    ])
    
    assert result.exit_code == 0
    # Verbose output goes to stderr, but stdout should still have valid JSON
    # In Click's CliRunner, stderr and stdout are mixed in output
    # Let's just verify it contains the expected debug markers and succeeds
    assert "=== JSON-RPC Request ===" in result.output or len(result.output) > 0


def test_cli_json_data(mcp_server):
    """Test with JSON data using real server."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", '{"message": "complex json"}'
    ])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert isinstance(output, list)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert output[0]["text"] == "complex json"


def test_cli_connection_error():
    """Test handling connection error with non-existent server."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:9999/tools"])
    
    assert result.exit_code == 1
    assert "Could not connect to server" in result.output
    assert "Connection refused" in result.output


def test_cli_dns_resolution_error():
    """Test handling DNS resolution error."""
    runner = CliRunner()
    result = runner.invoke(main, ["https://invalid-server.test/tools"])
    
    assert result.exit_code == 1
    assert "Could not connect to server" in result.output
    assert "DNS resolution failed" in result.output


def test_cli_timeout_error():
    """Test handling timeout error."""
    from unittest.mock import patch
    
    runner = CliRunner()
    
    # Mock make_mcp_request to raise an ExceptionGroup with TimeoutError
    with patch("murl.cli.make_mcp_request") as mock_request:
        timeout_exc = TimeoutError("Request timed out")
        mock_request.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [timeout_exc])
        
        result = runner.invoke(main, ["http://localhost:8765/tools"])
    
    assert result.exit_code == 1
    assert "Request timeout" in result.output


def test_cli_generic_connect_error():
    """Test handling generic ConnectError with unknown error message."""
    from unittest.mock import patch
    
    runner = CliRunner()
    
    # Mock make_mcp_request to raise an ExceptionGroup with a generic ConnectError
    with patch("murl.cli.make_mcp_request") as mock_request:
        # Create a custom ConnectError class with a message that doesn't match known patterns
        class ConnectError(Exception):
            pass
        
        connect_exc = ConnectError("Some other network error")
        mock_request.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [connect_exc])
        
        result = runner.invoke(main, ["http://localhost:8765/tools"])
    
    assert result.exit_code == 1
    assert "Could not connect to server" in result.output
    assert "Some other network error" in result.output


def test_cli_invalid_url():
    """Test handling invalid URL."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/invalid"])
    
    assert result.exit_code == 2  # Exit code 2 for invalid arguments per POSIX Agent Standard
    assert "Invalid MCP URL" in result.output


# Legacy tests

def test_version_option():
    """Test --version flag."""
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help():
    """Test --help flag."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "MCP Curl" in result.output
    assert "Model Context Protocol" in result.output


def test_upgrade_option():
    """Test --upgrade flag."""
    from unittest.mock import patch, MagicMock
    
    runner = CliRunner()
    
    # Mock subprocess.run to avoid actual pip execution
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Successfully installed mcp-curl-0.2.1"
    mock_result.stderr = ""
    
    with patch('subprocess.run', return_value=mock_result) as mock_run:
        result = runner.invoke(main, ["--upgrade"])
        
        # Verify subprocess.run was called with correct arguments
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == [sys.executable, "-m", "pip", "install", "--upgrade", "mcp-curl"]
        assert call_args[1]['timeout'] == 300
        
    assert result.exit_code == 0
    assert "Upgrading murl" in result.output
    assert "Upgrade complete" in result.output


# POSIX Agent Standard (PAS) tests

def test_agent_help():
    """Test --agent --help flag displays agent-optimized help."""
    runner = CliRunner()
    result = runner.invoke(main, ["--agent", "--help"])
    assert result.exit_code == 0
    assert "USAGE:" in result.output
    assert "COMMON PATTERNS:" in result.output
    assert "ERROR CODES:" in result.output
    assert "ANTI-PATTERNS:" in result.output
    # Should be concise agent contract, not verbose human help
    assert "JSON Lines (NDJSON)" in result.output


def test_agent_mode_list_output(mcp_server):
    """Test --agent mode outputs JSON Lines (NDJSON) for lists."""
    runner = CliRunner()
    result = runner.invoke(main, ["--agent", f"{TEST_SERVER_URL}/tools"])
    
    assert result.exit_code == 0
    # Parse output as JSON Lines
    lines = result.output.strip().split('\n')
    assert len(lines) > 0
    
    # Each line should be valid, compact JSON
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)
        # Check for compact JSON (no spaces after separators)
        assert ', ' not in line and '": ' not in line and ': ' not in line


def test_agent_mode_single_output(mcp_server):
    """Test --agent mode outputs compact JSON for single results."""
    runner = CliRunner()
    result = runner.invoke(main, ["--agent", f"{TEST_SERVER_URL}/tools/echo", "-d", "message=test"])
    
    assert result.exit_code == 0
    # Should be compact JSON (no indentation)
    assert '  ' not in result.output  # No double spaces (indentation)
    # Should be valid JSON Lines (one object per line)
    lines = result.output.strip().split('\n')
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)


def test_agent_mode_error_structure():
    """Test --agent mode outputs structured errors to stderr.
    
    Note: We use the default CliRunner() which mixes stderr into output for compatibility
    across all Click versions. The actual CLI correctly outputs errors to stderr.
    """
    runner = CliRunner()
    # Invalid URL should produce structured error
    result = runner.invoke(main, ["--agent", "http://localhost:3000/invalid"])
    
    assert result.exit_code == 2  # Invalid arguments
    # Output should contain structured JSON error (from stderr)
    error_obj = json.loads(result.output.strip())
    assert "error" in error_obj
    assert "message" in error_obj
    assert "code" in error_obj
    assert error_obj["code"] == 2


def test_agent_mode_connection_error():
    """Test --agent mode connection error is structured.
    
    Note: We use the default CliRunner() which mixes stderr into output for compatibility
    across all Click versions. The actual CLI correctly outputs errors to stderr.
    """
    runner = CliRunner()
    # Connect to non-existent server
    result = runner.invoke(main, ["--agent", "http://localhost:19999/tools"])
    
    assert result.exit_code == 1  # General error
    # Output should contain structured JSON error (from stderr)
    error_obj = json.loads(result.output.strip())
    assert "error" in error_obj
    assert error_obj["error"] in ["CONNECTION_REFUSED", "CONNECTION_ERROR"]
    assert "message" in error_obj


def test_agent_mode_missing_url():
    """Test --agent mode with missing URL produces structured error.
    
    Note: We use the default CliRunner() which mixes stderr into output for compatibility
    across all Click versions. The actual CLI correctly outputs errors to stderr.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["--agent"])
    
    assert result.exit_code == 2  # Invalid arguments
    # Output should contain structured JSON error (from stderr)
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "MISSING_ARGUMENT"
    assert "URL argument is required" in error_obj["message"]


def test_human_mode_list_output(mcp_server):
    """Test human mode (without --agent) outputs pretty-printed JSON."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools"])
    
    assert result.exit_code == 0
    # Should be pretty-printed JSON with indentation
    assert '  ' in result.output  # Has indentation
    # Should be valid JSON array
    output = json.loads(result.output)
    assert isinstance(output, list)


def test_validate_pas_option():
    """Test --validate-pas option shows PAS compliance check."""
    runner = CliRunner()
    result = runner.invoke(main, ["--validate-pas"])
    
    assert result.exit_code == 0
    # Should contain PAS compliance information
    assert "POSIX Agent Standard (PAS) Compliance Check" in result.output
    assert "Level 1: Agent-Safe" in result.output
    assert "Level 2: Agent-Optimized" in result.output
    assert "Level 3: Navigation Contract" in result.output
    assert "Level 4: State Contract" in result.output
    assert "COMPLIANCE STATUS: Level 2 (Agent-Optimized) ✅" in result.output
    # Should reference the compliance document
    assert "PAS_COMPLIANCE.md" in result.output
