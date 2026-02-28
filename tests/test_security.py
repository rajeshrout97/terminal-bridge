"""Tests for the security layer."""

import time

from terminal_bridge.security.tokens import (
    RateLimiter,
    compute_hmac,
    decode_pairing_code,
    generate_nonce,
    generate_pairing_code,
    generate_secret_key,
    verify_hmac,
)


def test_generate_secret_key():
    """Test secret key generation."""
    key = generate_secret_key()
    assert len(key) == 64  # 32 bytes = 64 hex chars
    assert key != generate_secret_key()  # Should be unique


def test_generate_nonce():
    """Test nonce generation."""
    nonce = generate_nonce()
    assert len(nonce) == 32  # 16 bytes = 32 hex chars
    assert nonce != generate_nonce()


def test_hmac_roundtrip():
    """Test HMAC computation and verification."""
    key = generate_secret_key()
    nonce = generate_nonce()
    signature = compute_hmac(key, nonce)
    assert verify_hmac(key, nonce, signature)


def test_hmac_wrong_key():
    """Test that wrong key fails verification."""
    key1 = generate_secret_key()
    key2 = generate_secret_key()
    nonce = generate_nonce()
    signature = compute_hmac(key1, nonce)
    assert not verify_hmac(key2, nonce, signature)


def test_hmac_wrong_nonce():
    """Test that wrong nonce fails verification."""
    key = generate_secret_key()
    nonce1 = generate_nonce()
    nonce2 = generate_nonce()
    signature = compute_hmac(key, nonce1)
    assert not verify_hmac(key, nonce2, signature)


def test_pairing_code_roundtrip():
    """Test pairing code encode/decode."""
    key = generate_secret_key()
    code = generate_pairing_code(key, "192.168.1.50", 9877, "Goku-MacBook")
    decoded = decode_pairing_code(code)
    assert decoded["k"] == key
    assert decoded["h"] == "192.168.1.50"
    assert decoded["p"] == 9877
    assert decoded["n"] == "Goku-MacBook"
    assert decoded["t"] > 0


def test_pairing_code_format():
    """Test pairing code is human-readable (dash-separated groups)."""
    key = generate_secret_key()
    code = generate_pairing_code(key, "10.0.0.1", 9877, "test")
    assert "-" in code
    parts = code.split("-")
    for part in parts:
        assert len(part) <= 4


def test_rate_limiter_allows():
    """Test rate limiter allows requests under the limit."""
    rl = RateLimiter(max_attempts=3, window_seconds=60)
    assert rl.check("client1")
    assert rl.check("client1")
    assert rl.check("client1")


def test_rate_limiter_blocks():
    """Test rate limiter blocks after exceeding limit."""
    rl = RateLimiter(max_attempts=2, window_seconds=60)
    assert rl.check("client1")
    assert rl.check("client1")
    assert not rl.check("client1")


def test_rate_limiter_reset():
    """Test rate limiter reset."""
    rl = RateLimiter(max_attempts=1, window_seconds=60)
    assert rl.check("client1")
    assert not rl.check("client1")
    rl.reset("client1")
    assert rl.check("client1")


def test_rate_limiter_separate_clients():
    """Test rate limiter tracks clients independently."""
    rl = RateLimiter(max_attempts=1, window_seconds=60)
    assert rl.check("client1")
    assert rl.check("client2")
    assert not rl.check("client1")
    assert not rl.check("client2")

