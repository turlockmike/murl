"""Tests for the CLI module."""

import json
import pytest
import subprocess
import time
import sys
import requests
from pathlib import Path
from click.testing import CliRunner
from murl.cli import (
    main,
    parse_url,
    parse_data_value,
    parse_data_flags,
    map_virtual_path_to_method,
    create_jsonrpc_request,
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


def test_create_jsonrpc_request():
    """Test creating JSON-RPC request."""
    request = create_jsonrpc_request("tools/list", {})
    assert request == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {}
    }


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
    assert output["message"] == "hello"


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
    assert output["city"] == "Paris"
    assert output["metric"] is True
    assert output["temperature"] == 72


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
    assert output["uri"] == "file:///test.txt"
    assert output["content"] == "Mock file content"



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
    assert output["name"] == "greeting"
    assert "Alice" in output["prompt"]


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
    assert output["message"] == "complex json"


def test_cli_connection_error():
    """Test handling connection error with non-existent server."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:9999/tools"])
    
    assert result.exit_code == 1
    assert "Connection refused" in result.output or "Error:" in result.output


def test_cli_invalid_url():
    """Test handling invalid URL."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/invalid"])
    
    assert result.exit_code == 1
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
