"""Credential storage for OAuth tokens.

Supports two backends (like gh CLI):
  1. System keychain (macOS Keychain, GNOME Keyring / KDE Wallet, Windows
     Credential Locker) via the ``keyring`` library.
  2. Plain JSON files in ~/.murl/credentials/ as a fallback when keyring
     is not installed or no secure backend is available.

Install keychain support with: pip install "mcp-curl[keychain]"
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


CREDENTIALS_DIR = Path.home() / ".murl" / "credentials"
EXPIRY_BUFFER_SECONDS = 60
_KEYRING_SERVICE = "murl"


def _key_for_url(server_url: str) -> str:
    """Return a SHA-256 hash of the server URL for use as a storage key."""
    return hashlib.sha256(server_url.encode()).hexdigest()


def _keyring_available() -> bool:
    """Check if keyring is installed and has a usable secure backend."""
    try:
        import keyring
        import keyring.errors

        backend = keyring.get_keyring()
        backend_mod = type(backend).__module__.lower()
        # The fail and null backends mean no real keychain is available.
        if "fail" in backend_mod or "null" in backend_mod:
            return False
        return True
    except Exception:
        return False


def _keyring_get(key: str) -> Optional[dict]:
    """Read credentials from the system keychain.

    Returns the parsed credentials dict, None if the key is not found,
    or raises on backend errors so the caller can decide how to handle
    the failure.
    """
    import keyring

    data = keyring.get_password(_KEYRING_SERVICE, key)
    if data is None:
        return None
    return json.loads(data)


def _keyring_set(key: str, creds: dict) -> bool:
    """Write credentials to the system keychain. Returns True on success."""
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, key, json.dumps(creds))
        return True
    except Exception:
        return False


def _keyring_delete(key: str) -> bool:
    """Delete credentials from the system keychain. Returns True on success."""
    try:
        import keyring
        import keyring.errors

        keyring.delete_password(_KEYRING_SERVICE, key)
        return True
    except keyring.errors.PasswordDeleteError:
        # Entry did not exist — nothing to delete, considered success.
        return True
    except Exception:
        return False


def _file_get(key: str) -> Optional[dict]:
    """Read credentials from a JSON file."""
    path = CREDENTIALS_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _file_set(key: str, creds: dict) -> None:
    """Write credentials to a JSON file with restrictive permissions."""
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CREDENTIALS_DIR, 0o700)
    except OSError:
        pass
    path = CREDENTIALS_DIR / f"{key}.json"
    # Create file with 0600 from the start to avoid a brief window where
    # tokens are world-readable under a permissive umask.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)


def _file_delete(key: str) -> bool:
    """Delete a credential file. Returns True on success."""
    try:
        (CREDENTIALS_DIR / f"{key}.json").unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_credentials(server_url: str) -> Optional[dict]:
    """Load stored credentials for a server URL, or None if not found.

    Tries the system keychain first, then falls back to the filesystem.
    """
    key = _key_for_url(server_url)
    if _keyring_available():
        try:
            creds = _keyring_get(key)
            if creds is not None:
                return creds
        except Exception:
            logger.warning(
                "Failed to read credentials from system keychain; "
                "falling back to file-based credential storage"
            )
    # Fallback (or migration path): check filesystem.
    return _file_get(key)


def save_credentials(server_url: str, creds: dict) -> None:
    """Persist credentials for a server URL.

    Saves to the system keychain if available, otherwise to a file.
    When saving to keychain, removes any stale credential file.
    """
    key = _key_for_url(server_url)
    data_to_save = dict(creds)
    data_to_save["server_url"] = server_url

    if _keyring_available() and _keyring_set(key, data_to_save):
        # Clean up any legacy file now that creds live in the keychain.
        _file_delete(key)
        return
    # Fallback: write to filesystem.
    _file_set(key, data_to_save)


def clear_credentials(server_url: str) -> bool:
    """Delete stored credentials for a server URL from all backends.

    Returns True if all applicable deletions succeeded.  Returns False
    (and logs a warning) if any backend failed to delete, but never
    raises so the logout flow is not interrupted.
    """
    key = _key_for_url(server_url)
    success = True

    if _keyring_available():
        if not _keyring_delete(key):
            logger.warning(
                "Failed to delete credentials from system keychain"
            )
            success = False

    if not _file_delete(key):
        logger.warning(
            "Failed to delete credentials from file storage"
        )
        success = False

    return success


def is_expired(creds: dict) -> bool:
    """Check if the access token is expired (with 60s buffer)."""
    expires_at = creds.get("expires_at")
    if expires_at is None:
        return True
    return time.time() >= (expires_at - EXPIRY_BUFFER_SECONDS)
