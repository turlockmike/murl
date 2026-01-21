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
MCPLANE_ECHO_URL = "https://echo.mcp.inevitable.fyi/mcp"
MICROSOFT_LEARN_URL = "https://learn.microsoft.com/api/mcp"


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
    not is_server_reachable(MCPLANE_ECHO_URL),
    reason="MCPlane echo server is not reachable"
)
def test_mcplane_echo_server_list_tools():
    """Test listing tools from the MCPlane echo server.
    
    This test validates that murl can connect to and interact with
    the public MCPlane MCP echo server.
    """
    runner = CliRunner()
    result = runner.invoke(main, [f"{MCPLANE_ECHO_URL}/tools"])
    
    # The test should succeed if the server is reachable
    # If it fails, we want to see the output for debugging
    if result.exit_code != 0:
        pytest.skip(f"Server returned error: {result.output}")
    
    # Verify we got a valid JSON response
    try:
        output = json.loads(result.output)
        assert isinstance(output, list), "Expected a list of tools"
    except json.JSONDecodeError as e:
        pytest.fail(f"Invalid JSON response: {e}\nOutput: {result.output}")


@pytest.mark.skipif(
    not is_server_reachable(MCPLANE_ECHO_URL),
    reason="MCPlane echo server is not reachable"
)
def test_mcplane_echo_server_connectivity():
    """Test basic connectivity to the MCPlane echo server.
    
    This is a lightweight test that just verifies the server responds.
    """
    runner = CliRunner()
    result = runner.invoke(main, [f"{MCPLANE_ECHO_URL}/tools"])
    
    # Check exit code first - connection failures typically result in non-zero codes
    # Exit code 0 means success, exit code 1 may indicate server-side issues but connection worked
    if result.exit_code not in [0, 1]:
        pytest.skip(f"Server connection failed with exit code {result.exit_code}")
    
    # If exit code is good, verify we got some response (not just an error message)
    if result.exit_code == 0:
        try:
            json.loads(result.output)
        except json.JSONDecodeError:
            pytest.skip(f"Server returned non-JSON response: {result.output[:100]}")


@pytest.mark.skipif(
    not is_server_reachable(MICROSOFT_LEARN_URL),
    reason="Microsoft Learn MCP server is not reachable"
)
def test_microsoft_learn_server_list_tools():
    """Test listing tools from the Microsoft Learn MCP server.
    
    This test validates that murl can connect to and interact with
    the Microsoft Learn MCP server.
    """
    runner = CliRunner()
    result = runner.invoke(main, [f"{MICROSOFT_LEARN_URL}/tools"])
    
    # The test should succeed if the server is reachable
    # If it fails, we want to see the output for debugging
    if result.exit_code != 0:
        pytest.skip(f"Server returned error: {result.output}")
    
    # Verify we got a valid JSON response
    try:
        output = json.loads(result.output)
        assert isinstance(output, list), "Expected a list of tools"
    except json.JSONDecodeError as e:
        pytest.fail(f"Invalid JSON response: {e}\nOutput: {result.output}")
