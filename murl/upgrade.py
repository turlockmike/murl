"""Upgrade command for murl."""

import subprocess
import sys

import click


# Repository configuration
GITHUB_REPO_URL = "https://raw.githubusercontent.com/turlockmike/murl/main/install.sh"


@click.command()
def upgrade():
    """Upgrade murl to the latest version from GitHub releases."""
    try:
        # Check if required tools are available
        try:
            subprocess.run(["curl", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            click.echo("Error: curl is not installed. Please install curl and try again.", err=True)
            sys.exit(1)
        
        try:
            subprocess.run(["bash", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            click.echo("Error: bash is not installed. Please install bash and try again.", err=True)
            sys.exit(1)
        
        click.echo("Upgrading murl...")
        click.echo("Downloading and running install script...")
        
        # Use subprocess with list arguments to avoid shell injection
        # First download the script
        download_result = subprocess.run(
            ["curl", "-sSL", GITHUB_REPO_URL],
            capture_output=True,
            text=True,
            check=False
        )
        
        if download_result.returncode != 0:
            click.echo(f"Error: Failed to download install script: {download_result.stderr}", err=True)
            sys.exit(1)
        
        # Then execute it with bash
        result = subprocess.run(
            ["bash"],
            input=download_result.stdout,
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode == 0:
            click.echo(result.stdout)
            click.echo("✓ Upgrade complete!")
        else:
            click.echo(f"Error during upgrade: {result.stderr}", err=True)
            sys.exit(1)
            
    except subprocess.SubprocessError as e:
        click.echo(f"Error: Subprocess failed: {e}", err=True)
        sys.exit(1)
    except PermissionError as e:
        click.echo(f"Error: Permission denied: {e}. You may need elevated privileges.", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: Unexpected error during upgrade: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    upgrade()
