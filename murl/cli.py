"""CLI entry point for murl."""

import click
from murl import __version__


@click.group()
@click.version_option(version=__version__)
def main():
    """murl - MCP Curl: A curl-like CLI tool for Model Context Protocol (MCP) servers.
    
    MCP (Model Context Protocol) is an open standard for AI models to access
    external data sources and tools. murl provides a command-line interface
    to interact with MCP servers.
    """
    pass


@main.command()
def version():
    """Display the version of murl."""
    click.echo(f"murl version {__version__}")


if __name__ == "__main__":
    main()
