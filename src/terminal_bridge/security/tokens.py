"""Token generation, HMAC signing, and verification for Terminal Bridge.

Authentication flow:
1. Server sends a random nonce (auth_challenge)
2. Client computes HMAC-SHA256(secret_key, nonce) and sends it back
3. Server verifies the HMAC
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time


def generate_secret_key() -> str:
    """Generate a 256-bit secret key as a hex string."""
    return secrets.token_hex(32)


def generate_nonce() -> str:
    """Generate a random nonce for authentication challenges."""
    return secrets.token_hex(16)


def compute_hmac(secret_key: str, nonce: str) -> str:
    """Compute HMAC-SHA256 of a nonce using the secret key."""
    return hmac.new(
        secret_key.encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_hmac(secret_key: str, nonce: str, signature: str) -> bool:
    """Verify an HMAC signature (constant-time comparison)."""
    expected = compute_hmac(secret_key, nonce)
    return hmac.compare_digest(expected, signature)


def generate_pairing_code(secret_key: str, host: str, port: int, hostname: str) -> str:
    """Generate a short pairing code that encodes connection info.

    Format: base64url encoded JSON with key, host, port, hostname.
    Displayed as groups of 4 chars separated by dashes for readability.
    """
    import base64
    import json

    data = json.dumps(
        {
            "k": secret_key,
            "h": host,
            "p": port,
            "n": hostname,
            "t": int(time.time()),
        },
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")
    # Format as groups of 4 for readability
    groups = [encoded[i : i + 4] for i in range(0, len(encoded), 4)]
    return "-".join(groups)


def decode_pairing_code(code: str) -> dict:
    """Decode a pairing code back to connection info.

    Returns dict with keys: k (secret_key), h (host), p (port), n (hostname), t (timestamp).
    """
    import base64
    import json

    # Remove formatting
    encoded = code.replace("-", "").replace(" ", "")
    # Re-add base64 padding
    padding = 4 - (len(encoded) % 4)
    if padding != 4:
        encoded += "=" * padding
    data = base64.urlsafe_b64decode(encoded).decode()
    return json.loads(data)


class RateLimiter:
    """Simple in-memory rate limiter for auth attempts."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()
        if key not in self._attempts:
            self._attempts[key] = []

        # Clean old entries
        self._attempts[key] = [
            t for t in self._attempts[key] if now - t < self.window_seconds
        ]

        if len(self._attempts[key]) >= self.max_attempts:
            return False

        self._attempts[key].append(now)
        return True

    def reset(self, key: str) -> None:
        """Reset rate limit for a key (e.g., after successful auth)."""
        self._attempts.pop(key, None)

