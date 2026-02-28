"""Tests for the protocol message layer."""

import json

from terminal_bridge.protocol.messages import (
    AuthChallenge,
    ExecRequest,
    ExecResult,
    Message,
    MessageType,
    make_message,
    parse_payload,
)


def test_message_creation():
    """Test creating a message with make_message."""
    msg = make_message(
        MessageType.EXEC_REQUEST,
        ExecRequest(command="ls -la", timeout=10),
    )
    assert msg.type == MessageType.EXEC_REQUEST
    assert msg.payload["command"] == "ls -la"
    assert msg.payload["timeout"] == 10
    assert msg.id  # Has an ID
    assert msg.timestamp > 0


def test_message_serialization():
    """Test JSON serialization roundtrip."""
    msg = make_message(
        MessageType.EXEC_REQUEST,
        ExecRequest(command="echo hello", timeout=5),
    )
    json_str = msg.to_json()
    parsed = Message.from_json(json_str)
    assert parsed.type == msg.type
    assert parsed.id == msg.id
    assert parsed.payload["command"] == "echo hello"


def test_parse_payload():
    """Test extracting a typed payload from a message."""
    msg = make_message(
        MessageType.EXEC_RESULT,
        ExecResult(
            stdout="hello\n",
            stderr="",
            exit_code=0,
            duration=0.1,
        ),
    )
    result = parse_payload(msg, ExecResult)
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    assert result.duration == 0.1
    assert result.timed_out is False


def test_all_message_types_exist():
    """Ensure all expected message types are defined."""
    expected = [
        "auth_challenge", "auth_response", "auth_result",
        "session_create", "session_created", "session_destroy",
        "terminal_input", "terminal_output", "terminal_resize",
        "exec_request", "exec_result",
        "file_push", "file_pull_request", "file_pull_result",
        "ping", "pong", "heartbeat",
        "relay_register", "relay_join", "relay_paired",
        "error",
    ]
    for name in expected:
        assert name in [m.value for m in MessageType], f"Missing: {name}"


def test_auth_challenge_payload():
    """Test AuthChallenge payload creation."""
    challenge = AuthChallenge(nonce="abc123", server_version="0.1.0")
    msg = make_message(MessageType.AUTH_CHALLENGE, challenge)
    parsed = parse_payload(msg, AuthChallenge)
    assert parsed.nonce == "abc123"
    assert parsed.server_version == "0.1.0"

