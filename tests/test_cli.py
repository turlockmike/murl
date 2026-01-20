"""Tests for the CLI module."""

import json
import pytest
from click.testing import CliRunner
from unittest.mock import patch, Mock
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
    """Test mapping /resources/read to resources/read."""
    data = {"uri": "file:///path/to/file"}
    method, params = map_virtual_path_to_method("/resources/read", data)
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file"}


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


# Integration tests with mocked HTTP

@patch('murl.cli.requests.post')
def test_cli_list_tools(mock_post):
    """Test listing tools."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": [{"name": "echo"}, {"name": "weather"}]
    }
    mock_post.return_value = mock_response
    
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/tools"])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "echo"


@patch('murl.cli.requests.post')
def test_cli_call_tool_with_data(mock_post):
    """Test calling a tool with data."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"message": "hello"}
    }
    mock_post.return_value = mock_response
    
    runner = CliRunner()
    result = runner.invoke(main, [
        "http://localhost:3000/tools/echo",
        "-d", "message=hello"
    ])
    
    assert result.exit_code == 0
    
    # Check that the request was made correctly
    call_args = mock_post.call_args
    assert call_args[0][0] == "http://localhost:3000"
    assert call_args[1]["json"]["method"] == "tools/call"
    assert call_args[1]["json"]["params"]["name"] == "echo"
    assert call_args[1]["json"]["params"]["arguments"]["message"] == "hello"


@patch('murl.cli.requests.post')
def test_cli_with_headers(mock_post):
    """Test with custom headers."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": []
    }
    mock_post.return_value = mock_response
    
    runner = CliRunner()
    result = runner.invoke(main, [
        "http://localhost:3000/prompts",
        "-H", "Authorization: Bearer token123"
    ])
    
    assert result.exit_code == 0
    
    # Check headers
    call_args = mock_post.call_args
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer token123"
    assert headers["Content-Type"] == "application/json"


@patch('murl.cli.requests.post')
def test_cli_verbose_mode(mock_post):
    """Test verbose mode."""
    mock_response = Mock()
    mock_response.headers = {"Content-Type": "application/json"}
    mock_response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": []
    }
    mock_post.return_value = mock_response
    
    runner = CliRunner()
    result = runner.invoke(main, [
        "http://localhost:3000/tools",
        "-v"
    ])
    
    assert result.exit_code == 0
    # Verbose output goes to stderr, so we can't easily check it in this test
    # But we can verify it didn't crash


@patch('murl.cli.requests.post')
def test_cli_json_rpc_error(mock_post):
    """Test handling JSON-RPC error."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32601,
            "message": "Method not found"
        }
    }
    mock_post.return_value = mock_response
    
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/tools"])
    
    assert result.exit_code == 1
    assert "Method not found" in result.output


@patch('murl.cli.requests.post')
def test_cli_connection_error(mock_post):
    """Test handling connection error."""
    mock_post.side_effect = Exception("Connection refused")
    
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/tools"])
    
    assert result.exit_code == 1


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
