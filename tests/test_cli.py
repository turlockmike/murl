"""Tests for the CLI module."""

import pytest
from click.testing import CliRunner
from murl.cli import main
from murl import __version__


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


def test_version_command():
    """Test version subcommand."""
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output
