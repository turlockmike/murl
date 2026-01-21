"""Optional integration tests for public MCP servers.

These tests validate connectivity to public MCP servers.
They can be skipped in CI if the servers are unreachable.

NOTE: Currently, there are no known working public MCP servers available for testing.
If you know of a public MCP server, you can add tests here following this pattern:

import json
import pytest
import requests
from click.testing import CliRunner
from murl.cli import main

# Example server configuration
# PUBLIC_SERVER_URL = "https://example.com/mcp"

def is_server_reachable(url: str) -> bool:
    '''Check if the server is reachable.'''
    try:
        response = requests.get(url, timeout=5)
        return response.status_code < 500
    except (requests.ConnectionError, requests.Timeout):
        return False

@pytest.mark.skipif(
    not is_server_reachable(PUBLIC_SERVER_URL),
    reason="Public server is not reachable"
)
def test_public_server_list_tools():
    '''Test listing tools from a public server.'''
    runner = CliRunner()
    result = runner.invoke(main, [f"{PUBLIC_SERVER_URL}/tools"])
    
    if result.exit_code != 0:
        pytest.skip(f"Server returned error: {result.output}")
    
    try:
        output = json.loads(result.output)
        assert isinstance(output, list), "Expected a list of tools"
    except json.JSONDecodeError as e:
        pytest.fail(f"Invalid JSON response: {e}\nOutput: {result.output}")
"""
