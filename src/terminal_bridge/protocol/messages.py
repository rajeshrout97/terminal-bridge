"""JSON message protocol for Terminal Bridge communication.

All messages are JSON objects with a "type" field and a "payload" field.
Messages are exchanged over WebSocket between the local bridge and remote agent.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """All supported message types."""

    # Authentication
    AUTH_CHALLENGE = "auth_challenge"
    AUTH_RESPONSE = "auth_response"
    AUTH_RESULT = "auth_result"

    # Session management
    SESSION_CREATE = "session_create"
    SESSION_CREATED = "session_created"
    SESSION_DESTROY = "session_destroy"
    SESSION_DESTROYED = "session_destroyed"
    SESSION_LIST = "session_list"
    SESSION_LIST_RESULT = "session_list_result"

    # Terminal I/O
    TERMINAL_INPUT = "terminal_input"
    TERMINAL_OUTPUT = "terminal_output"
    TERMINAL_RESIZE = "terminal_resize"
    TERMINAL_EXIT = "terminal_exit"

    # Command execution (one-shot)
    EXEC_REQUEST = "exec_request"
    EXEC_RESULT = "exec_result"

    # File transfer
    FILE_PUSH = "file_push"
    FILE_PULL_REQUEST = "file_pull_request"
    FILE_PULL_RESULT = "file_pull_result"
    FILE_RESULT = "file_result"

    # System info
    SYSTEM_INFO_REQUEST = "system_info_request"
    SYSTEM_INFO_RESULT = "system_info_result"

    # Health
    PING = "ping"
    PONG = "pong"
    HEARTBEAT = "heartbeat"

    # Relay
    RELAY_REGISTER = "relay_register"
    RELAY_JOIN = "relay_join"
    RELAY_PAIRED = "relay_paired"
    RELAY_DATA = "relay_data"
    RELAY_ERROR = "relay_error"

    # Error
    ERROR = "error"


class Message(BaseModel):
    """Base message envelope."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: MessageType
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> Message:
        return cls.model_validate_json(data)


# ---------------------------------------------------------------------------
# Authentication messages
# ---------------------------------------------------------------------------
class AuthChallenge(BaseModel):
    """Server sends a random nonce for HMAC authentication."""

    nonce: str
    server_version: str


class AuthResponse(BaseModel):
    """Client responds with HMAC of the nonce."""

    hmac_signature: str
    client_version: str
    hostname: str


class AuthResult(BaseModel):
    """Server confirms or denies authentication."""

    success: bool
    message: str = ""


# ---------------------------------------------------------------------------
# Session messages
# ---------------------------------------------------------------------------
class SessionCreate(BaseModel):
    """Request to create a new PTY session."""

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    shell: str | None = None  # Use default shell if None
    cols: int = 80
    rows: int = 24
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class SessionCreated(BaseModel):
    """Confirms a new session was created."""

    session_id: str
    shell: str
    pid: int


class SessionDestroy(BaseModel):
    """Request to destroy a session."""

    session_id: str


class SessionDestroyed(BaseModel):
    """Confirms a session was destroyed."""

    session_id: str
    exit_code: int | None = None


class SessionInfo(BaseModel):
    """Information about a single session."""

    id: str
    shell: str
    pid: int
    started: str
    idle: str
    cols: int
    rows: int


class SessionListResult(BaseModel):
    """List of active sessions."""

    sessions: list[SessionInfo]


# ---------------------------------------------------------------------------
# Terminal I/O messages
# ---------------------------------------------------------------------------
class TerminalInput(BaseModel):
    """Input (keystrokes) to send to a PTY session."""

    session_id: str
    data: str  # base64 encoded bytes


class TerminalOutput(BaseModel):
    """Output from a PTY session."""

    session_id: str
    data: str  # base64 encoded bytes


class TerminalResize(BaseModel):
    """Resize a PTY session."""

    session_id: str
    cols: int
    rows: int


class TerminalExit(BaseModel):
    """A PTY session exited."""

    session_id: str
    exit_code: int


# ---------------------------------------------------------------------------
# One-shot execution messages
# ---------------------------------------------------------------------------
class ExecRequest(BaseModel):
    """Execute a command and return the result."""

    command: str
    timeout: float = 30.0
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    session_id: str | None = None  # If set, run in existing session


class ExecResult(BaseModel):
    """Result of a one-shot command execution."""

    stdout: str
    stderr: str
    exit_code: int
    duration: float
    timed_out: bool = False


# ---------------------------------------------------------------------------
# File transfer messages
# ---------------------------------------------------------------------------
class FilePush(BaseModel):
    """Push a file to the remote Mac."""

    path: str
    content: str  # base64 encoded
    mode: int = 0o644


class FilePullRequest(BaseModel):
    """Request to pull a file from the remote Mac."""

    path: str


class FilePullResult(BaseModel):
    """Result of a file pull."""

    path: str
    content: str  # base64 encoded
    size: int
    mode: int


class FileResult(BaseModel):
    """Generic file operation result."""

    success: bool
    message: str = ""


# ---------------------------------------------------------------------------
# System info messages
# ---------------------------------------------------------------------------
class SystemInfoResult(BaseModel):
    """System information about the remote Mac."""

    hostname: str
    os_version: str
    architecture: str
    cpu_count: int
    memory_total_gb: float
    memory_available_gb: float
    disk_total_gb: float
    disk_free_gb: float
    python_version: str
    shell: str
    username: str
    uptime: str


# ---------------------------------------------------------------------------
# Health messages
# ---------------------------------------------------------------------------
class Ping(BaseModel):
    """Ping message for latency measurement."""

    sent_at: float = Field(default_factory=time.time)


class Pong(BaseModel):
    """Pong response."""

    sent_at: float  # Echo back the original timestamp
    server_time: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Relay messages
# ---------------------------------------------------------------------------
class RelayRegister(BaseModel):
    """Agent registers with the relay server."""

    room_id: str
    auth_token: str


class RelayJoin(BaseModel):
    """Client joins a relay room."""

    room_id: str
    auth_token: str


class RelayPaired(BaseModel):
    """Relay confirms both sides are connected."""

    room_id: str
    peer_hostname: str


class RelayError(BaseModel):
    """Relay error message."""

    code: str
    message: str


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------
class ErrorPayload(BaseModel):
    """Generic error."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helper to build messages
# ---------------------------------------------------------------------------
def make_message(msg_type: MessageType, payload_model: BaseModel | None = None) -> Message:
    """Create a Message with the given type and payload."""
    payload = payload_model.model_dump() if payload_model else {}
    return Message(type=msg_type, payload=payload)


def parse_payload(msg: Message, model_class: type[BaseModel]) -> BaseModel:
    """Parse a message payload into a specific model."""
    return model_class.model_validate(msg.payload)

