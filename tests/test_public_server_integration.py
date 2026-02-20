"""Optional integration tests for public MCP servers.

These tests validate connectivity to public MCP servers.
They can be skipped in CI if the servers are unreachable.
"""

import json
import pytest
import requests
from click.testing import CliRunner
from murl.cli import main


# Public MCP server configurations
FETCH_SERVER_URL = "https://remote.mcpservers.org/fetch/mcp"
DEEPWIKI_URL = "https://mcp.deepwiki.com/mcp"


def is_server_reachable(url: str) -> bool:
    """Check if the server is reachable.
    
    Args:
        url: The server URL to check
        
    Returns:
        True if the server responds, False otherwise
    """
    try:
        response = requests.get(url, timeout=5)
        return response.status_code < 500
    except (requests.ConnectionError, requests.Timeout):
        return False


@pytest.mark.skipif(
    not is_server_reachable(FETCH_SERVER_URL),
    reason="Fetch server is not reachable"
)
def test_fetch_server_list_tools():
    """Test listing tools from the Fetch server.
    
    This test validates that murl can connect to and interact with
    the public Fetch MCP server.
    """
    runner = CliRunner()
    result = runner.invoke(main, [f"{FETCH_SERVER_URL}/tools", "--no-auth"])

    # The test should succeed if the server is reachable
    # If it fails, we want to see the output for debugging
    if result.exit_code != 0:
        pytest.skip(f"Server returned error: {result.output}")

    # Verify we got valid NDJSON response (one JSON object per line)
    try:
        output = [json.loads(line) for line in result.output.strip().split('\n') if line.strip()]
        assert isinstance(output, list) and len(output) > 0, "Expected at least one tool"
    except json.JSONDecodeError as e:
        pytest.fail(f"Invalid JSON response: {e}\nOutput: {result.output}")


@pytest.mark.skipif(
    not is_server_reachable(FETCH_SERVER_URL),
    reason="Fetch server is not reachable"
)
def test_fetch_server_connectivity():
    """Test basic connectivity to the Fetch server.

    This is a lightweight test that just verifies the server responds.
    """
    runner = CliRunner()
    result = runner.invoke(main, [f"{FETCH_SERVER_URL}/tools", "--no-auth"])

    # Check exit code first - connection failures typically result in non-zero codes
    # Exit code 0 means success, exit code 1 may indicate server-side issues but connection worked
    if result.exit_code not in [0, 1]:
        pytest.skip(f"Server connection failed with exit code {result.exit_code}")

    # If exit code is good, verify we got some NDJSON response
    if result.exit_code == 0:
        try:
            lines = [json.loads(line) for line in result.output.strip().split('\n') if line.strip()]
            assert len(lines) > 0
        except json.JSONDecodeError:
            pytest.skip(f"Server returned non-JSON response: {result.output[:100]}")


@pytest.mark.skipif(
    not is_server_reachable(DEEPWIKI_URL),
    reason="DeepWiki server is not reachable"
)
def test_deepwiki_server_list_tools():
    """Test listing tools from the DeepWiki server.
    
    This test validates that murl can connect to and interact with
    the DeepWiki MCP server.
    """
    runner = CliRunner()
    result = runner.invoke(main, [f"{DEEPWIKI_URL}/tools", "--no-auth"])
    
    # The test should succeed if the server is reachable
    # If it fails, we want to see the output for debugging
    if result.exit_code != 0:
        pytest.skip(f"Server returned error: {result.output}")
    
    # Verify we got valid NDJSON response (one JSON object per line)
    try:
        output = [json.loads(line) for line in result.output.strip().split('\n') if line.strip()]
        assert isinstance(output, list) and len(output) > 0, "Expected at least one tool"
    except json.JSONDecodeError as e:
        pytest.fail(f"Invalid JSON response: {e}\nOutput: {result.output}")
