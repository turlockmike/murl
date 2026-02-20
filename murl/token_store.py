"""Credential storage for OAuth tokens."""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional


CREDENTIALS_DIR = Path.home() / ".murl" / "credentials"
EXPIRY_BUFFER_SECONDS = 60


def _key_for_url(server_url: str) -> str:
    """Return a SHA-256 hash of the server URL for use as a filename."""
    return hashlib.sha256(server_url.encode()).hexdigest()


def get_credentials(server_url: str) -> Optional[dict]:
    """Load stored credentials for a server URL, or None if not found."""
    path = CREDENTIALS_DIR / f"{_key_for_url(server_url)}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_credentials(server_url: str, creds: dict) -> None:
    """Persist credentials for a server URL."""
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    path = CREDENTIALS_DIR / f"{_key_for_url(server_url)}.json"
    creds["server_url"] = server_url
    with open(path, "w") as f:
        json.dump(creds, f, indent=2)
    # Restrict permissions to owner only
    os.chmod(path, 0o600)


def clear_credentials(server_url: str) -> None:
    """Delete stored credentials for a server URL."""
    path = CREDENTIALS_DIR / f"{_key_for_url(server_url)}.json"
    if path.exists():
        path.unlink()


def is_expired(creds: dict) -> bool:
    """Check if the access token is expired (with 60s buffer)."""
    expires_at = creds.get("expires_at")
    if expires_at is None:
        return True
    return time.time() >= (expires_at - EXPIRY_BUFFER_SECONDS)
