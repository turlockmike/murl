"""Upgrade command for murl."""

import subprocess
import sys

import click


@click.command()
def upgrade():
    """Upgrade murl to the latest version from GitHub releases."""
    try:
        click.echo("Upgrading murl...")
        click.echo("Downloading and running install script...")
        
        # Download and run the install script
        install_url = "https://raw.githubusercontent.com/turlockmike/murl/main/install.sh"
        
        # Use curl to download and pipe to bash
        result = subprocess.run(
            ["bash", "-c", f"curl -sSL {install_url} | bash"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            click.echo(result.stdout)
            click.echo("✓ Upgrade complete!")
        else:
            click.echo(f"Error during upgrade: {result.stderr}", err=True)
            sys.exit(1)
            
    except Exception as e:
        click.echo(f"Error: Failed to upgrade: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    upgrade()
