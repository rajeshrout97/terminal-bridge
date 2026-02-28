"""macOS Keychain wrapper for securely storing Terminal Bridge secrets.

Uses the `keyring` library which maps to macOS Keychain on macOS.
Fallback to file-based storage if Keychain is unavailable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from terminal_bridge.config import SERVICE_NAME, get_config_dir

logger = logging.getLogger(__name__)

_KEYCHAIN_AVAILABLE = True
try:
    import keyring
except ImportError:
    _KEYCHAIN_AVAILABLE = False


def store_secret(key_name: str, secret: str) -> None:
    """Store a secret in macOS Keychain (or fallback to file)."""
    if _KEYCHAIN_AVAILABLE:
        try:
            keyring.set_password(SERVICE_NAME, key_name, secret)
            logger.debug("Stored %s in macOS Keychain", key_name)
            return
        except Exception as e:
            logger.warning("Keychain unavailable (%s), falling back to file storage", e)

    _store_secret_file(key_name, secret)


def retrieve_secret(key_name: str) -> str | None:
    """Retrieve a secret from macOS Keychain (or fallback to file)."""
    if _KEYCHAIN_AVAILABLE:
        try:
            secret = keyring.get_password(SERVICE_NAME, key_name)
            if secret is not None:
                return secret
        except Exception as e:
            logger.warning("Keychain read failed (%s), trying file fallback", e)

    return _retrieve_secret_file(key_name)


def delete_secret(key_name: str) -> None:
    """Delete a secret from macOS Keychain."""
    if _KEYCHAIN_AVAILABLE:
        try:
            keyring.delete_password(SERVICE_NAME, key_name)
        except Exception:
            pass
    _delete_secret_file(key_name)


# ---------------------------------------------------------------------------
# File-based fallback (permissions-restricted)
# ---------------------------------------------------------------------------
def _secrets_dir() -> Path:
    d = get_config_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    return d


def _store_secret_file(key_name: str, secret: str) -> None:
    path = _secrets_dir() / f"{key_name}.key"
    path.write_text(secret)
    path.chmod(0o600)
    logger.debug("Stored %s in file: %s", key_name, path)


def _retrieve_secret_file(key_name: str) -> str | None:
    path = _secrets_dir() / f"{key_name}.key"
    if path.exists():
        return path.read_text().strip()
    return None


def _delete_secret_file(key_name: str) -> None:
    path = _secrets_dir() / f"{key_name}.key"
    if path.exists():
        path.unlink()

