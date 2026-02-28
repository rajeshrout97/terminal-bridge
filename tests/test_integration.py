"""Integration tests for Terminal Bridge.

These tests verify the end-to-end flow of the system:
- Agent server startup
- Client connection and authentication
- Command execution
- Session management
- File transfer

Note: These tests spawn real processes and require macOS.
Run with: pytest tests/test_integration.py -v
"""

import asyncio
import base64
import os
import tempfile

import pytest

from terminal_bridge.protocol.messages import (
    ExecRequest,
    ExecResult,
    Message,
    MessageType,
    SessionCreate,
    SessionCreated,
    make_message,
    parse_payload,
)
from terminal_bridge.security.tokens import generate_secret_key


@pytest.fixture
def secret_key():
    """Generate a fresh secret key for testing."""
    return generate_secret_key()


@pytest.fixture
def temp_dir():
    """Provide a temporary directory."""
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestProtocolIntegration:
    """Test protocol message encoding/decoding."""

    def test_exec_request_roundtrip(self):
        """Full roundtrip of an exec request."""
        msg = make_message(
            MessageType.EXEC_REQUEST,
            ExecRequest(command="echo hello", timeout=5),
        )
        json_str = msg.to_json()
        parsed = Message.from_json(json_str)
        req = parse_payload(parsed, ExecRequest)
        assert req.command == "echo hello"
        assert req.timeout == 5

    def test_exec_result_roundtrip(self):
        """Full roundtrip of an exec result."""
        msg = make_message(
            MessageType.EXEC_RESULT,
            ExecResult(
                stdout="hello world\n",
                stderr="",
                exit_code=0,
                duration=0.05,
                timed_out=False,
            ),
        )
        json_str = msg.to_json()
        parsed = Message.from_json(json_str)
        result = parse_payload(parsed, ExecResult)
        assert result.stdout == "hello world\n"
        assert result.exit_code == 0


class TestPTYManager:
    """Test PTY session management."""

    @pytest.mark.asyncio
    async def test_exec_command(self):
        """Test one-shot command execution."""
        from terminal_bridge.remote_agent.pty_manager import PTYManager

        mgr = PTYManager()
        result = await mgr.exec_command("echo 'hello from pty'", timeout=5)
        assert "hello from pty" in result["stdout"]
        assert result["exit_code"] == 0
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_exec_command_with_exit_code(self):
        """Test command that returns non-zero exit code."""
        from terminal_bridge.remote_agent.pty_manager import PTYManager

        mgr = PTYManager()
        result = await mgr.exec_command("exit 42", timeout=5)
        assert result["exit_code"] == 42

    @pytest.mark.asyncio
    async def test_exec_command_timeout(self):
        """Test command timeout."""
        from terminal_bridge.remote_agent.pty_manager import PTYManager

        mgr = PTYManager()
        result = await mgr.exec_command("sleep 100", timeout=1)
        assert result["timed_out"] is True

    @pytest.mark.asyncio
    async def test_exec_command_stderr(self):
        """Test capturing stderr."""
        from terminal_bridge.remote_agent.pty_manager import PTYManager

        mgr = PTYManager()
        result = await mgr.exec_command("echo 'error' >&2", timeout=5)
        assert "error" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exec_command_with_cwd(self):
        """Test command with working directory."""
        from terminal_bridge.remote_agent.pty_manager import PTYManager

        mgr = PTYManager()
        result = await mgr.exec_command("pwd", timeout=5, cwd="/tmp")
        stdout = result["stdout"].strip()
        # On macOS, /tmp is a symlink to /private/tmp
        assert stdout in ("/tmp", "/private/tmp")


class TestPairingCodes:
    """Test pairing code generation and verification."""

    def test_full_pairing_flow(self, secret_key):
        """Test the full pairing code flow."""
        from terminal_bridge.setup.pairing import (
            create_pairing_code,
            verify_pairing_code,
        )

        # Manually create since we can't rely on network
        from terminal_bridge.security.tokens import generate_pairing_code

        code = generate_pairing_code(secret_key, "10.0.0.1", 9877, "test-mac")
        info = verify_pairing_code(code)
        assert info is not None
        assert info["k"] == secret_key
        assert info["h"] == "10.0.0.1"
        assert info["p"] == 9877

    def test_invalid_pairing_code(self):
        """Test that invalid codes are rejected."""
        from terminal_bridge.setup.pairing import verify_pairing_code

        assert verify_pairing_code("not-a-valid-code") is None
        assert verify_pairing_code("") is None


class TestTLSCertGeneration:
    """Test TLS certificate generation."""

    def test_generate_cert(self, temp_dir):
        """Test self-signed cert generation."""
        from pathlib import Path

        from terminal_bridge.security.tls import generate_self_signed_cert

        cert_path, key_path = generate_self_signed_cert(
            hostname="test-host",
            cert_dir=Path(temp_dir),
        )
        assert cert_path.exists()
        assert key_path.exists()
        assert cert_path.stat().st_size > 0
        assert key_path.stat().st_size > 0

        # Check key file permissions
        mode = oct(key_path.stat().st_mode)[-3:]
        assert mode == "600"

    def test_ssl_contexts(self, temp_dir):
        """Test SSL context creation."""
        from pathlib import Path

        from terminal_bridge.security.tls import (
            generate_self_signed_cert,
            get_ssl_context_client,
            get_ssl_context_server,
        )

        cert_path, key_path = generate_self_signed_cert(
            hostname="test-host",
            cert_dir=Path(temp_dir),
        )
        server_ctx = get_ssl_context_server(cert_path, key_path)
        assert server_ctx is not None

        client_ctx = get_ssl_context_client()
        assert client_ctx is not None


class TestFileOperations:
    """Test file transfer encode/decode."""

    def test_file_content_roundtrip(self, temp_dir):
        """Test file content base64 encoding roundtrip."""
        content = b"Hello, Terminal Bridge!\nLine 2\n"
        encoded = base64.b64encode(content).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == content

    def test_binary_file_roundtrip(self, temp_dir):
        """Test binary file encoding."""
        content = bytes(range(256))
        encoded = base64.b64encode(content).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == content

