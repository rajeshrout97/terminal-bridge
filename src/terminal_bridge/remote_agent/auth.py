"""Authentication handler for the remote agent WebSocket server.

Implements the HMAC challenge-response protocol:
1. Server sends nonce
2. Client sends HMAC(secret, nonce)
3. Server verifies
"""

from __future__ import annotations

import logging

import websockets.asyncio.server

from terminal_bridge.protocol.messages import (
    AuthChallenge,
    AuthResponse,
    AuthResult,
    Message,
    MessageType,
    make_message,
    parse_payload,
)
from terminal_bridge.security.keychain import retrieve_secret
from terminal_bridge.security.tokens import RateLimiter, generate_nonce, verify_hmac
from terminal_bridge.version import __version__

logger = logging.getLogger(__name__)

_rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)


async def authenticate_client(
    websocket: websockets.asyncio.server.ServerConnection,
) -> bool:
    """Run the authentication handshake with a connecting client.

    Returns True if authentication succeeds, False otherwise.
    """
    remote_addr = websocket.remote_address
    client_key = f"{remote_addr[0]}:{remote_addr[1]}" if remote_addr else "unknown"

    # Rate limit check
    if not _rate_limiter.check(client_key):
        logger.warning("Rate limited: %s", client_key)
        fail = make_message(
            MessageType.AUTH_RESULT,
            AuthResult(success=False, message="Rate limited. Try again later."),
        )
        await websocket.send(fail.to_json())
        return False

    # Get secret key
    secret_key = retrieve_secret("agent_key")
    if not secret_key:
        logger.error("No agent key found in Keychain/config. Run 'tbridge setup remote' first.")
        fail = make_message(
            MessageType.AUTH_RESULT,
            AuthResult(success=False, message="Server not configured."),
        )
        await websocket.send(fail.to_json())
        return False

    # Send challenge
    nonce = generate_nonce()
    challenge = make_message(
        MessageType.AUTH_CHALLENGE,
        AuthChallenge(nonce=nonce, server_version=__version__),
    )
    await websocket.send(challenge.to_json())

    # Wait for response (5 second timeout)
    try:
        import asyncio

        raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        msg = Message.from_json(raw)
    except Exception as e:
        logger.warning("Auth timeout/error from %s: %s", client_key, e)
        return False

    if msg.type != MessageType.AUTH_RESPONSE:
        logger.warning("Unexpected message type from %s: %s", client_key, msg.type)
        return False

    response = parse_payload(msg, AuthResponse)

    # Verify HMAC
    if verify_hmac(secret_key, nonce, response.hmac_signature):
        _rate_limiter.reset(client_key)
        result = make_message(
            MessageType.AUTH_RESULT,
            AuthResult(success=True, message="Authenticated"),
        )
        await websocket.send(result.to_json())
        logger.info("Authenticated client %s (hostname=%s)", client_key, response.hostname)
        return True
    else:
        result = make_message(
            MessageType.AUTH_RESULT,
            AuthResult(success=False, message="Invalid credentials"),
        )
        await websocket.send(result.to_json())
        logger.warning("Auth failed for %s", client_key)
        return False

