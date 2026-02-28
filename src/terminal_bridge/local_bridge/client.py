"""Local bridge WebSocket client.

Connects to the remote agent (directly or via relay) and provides
the Python SDK API (RemoteTerminal) as well as functions used by the CLI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
import time
from pathlib import Path
from typing import Any, Callable

import websockets.asyncio.client

from terminal_bridge.config import (
    DEFAULT_AGENT_PORT,
    add_remote_config,
    get_remote_config,
    load_config,
)
from terminal_bridge.protocol.messages import (
    AuthChallenge,
    AuthResponse,
    AuthResult,
    ExecRequest,
    ExecResult,
    FilePullRequest,
    FilePullResult,
    FilePush,
    FileResult,
    Message,
    MessageType,
    Ping,
    Pong,
    SessionCreate,
    SessionCreated,
    SessionDestroy,
    SessionDestroyed,
    SessionListResult,
    TerminalInput,
    TerminalOutput,
    TerminalResize,
    make_message,
    parse_payload,
)
from terminal_bridge.security.keychain import retrieve_secret
from terminal_bridge.security.tokens import compute_hmac
from terminal_bridge.security.tls import get_ssl_context_client
from terminal_bridge.version import __version__

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages the WebSocket connection to a remote agent with auto-reconnect."""

    def __init__(
        self,
        url: str,
        secret_key: str,
        use_tls: bool = True,
        auto_reconnect: bool = True,
    ) -> None:
        self.url = url
        self.secret_key = secret_key
        self.use_tls = use_tls
        self.auto_reconnect = auto_reconnect
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._connected = asyncio.Event()
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._output_callbacks: dict[str, Callable] = {}
        self._exit_callbacks: dict[str, Callable] = {}
        self._reader_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    async def connect(self) -> None:
        """Establish connection and authenticate."""
        ssl_context = get_ssl_context_client() if self.use_tls else None
        try:
            self._ws = await websockets.asyncio.client.connect(
                self.url,
                ssl=ssl_context,
                max_size=50 * 1024 * 1024,  # 50MB max message
                ping_interval=20,
                ping_timeout=10,
            )
        except Exception as e:
            logger.error("Connection failed: %s", e)
            raise ConnectionError(f"Failed to connect to {self.url}: {e}") from e

        # Authenticate
        authenticated = await self._authenticate()
        if not authenticated:
            await self._ws.close()
            self._ws = None
            raise ConnectionError("Authentication failed")

        self._connected.set()
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("Connected to %s", self.url)

    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected.clear()

    async def send_and_wait(self, msg: Message, timeout: float = 30.0) -> Message:
        """Send a message and wait for the correlated response."""
        if not self.connected:
            await self._wait_connected(timeout=5.0)

        future: asyncio.Future[Message] = asyncio.get_event_loop().create_future()
        self._pending_responses[msg.id] = future

        try:
            await self._ws.send(msg.to_json())  # type: ignore
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_responses.pop(msg.id, None)
            raise TimeoutError(f"No response within {timeout}s")
        except Exception:
            self._pending_responses.pop(msg.id, None)
            raise

    async def send_fire_forget(self, msg: Message) -> None:
        """Send a message without waiting for a response."""
        if not self.connected:
            await self._wait_connected(timeout=5.0)
        await self._ws.send(msg.to_json())  # type: ignore

    def on_output(self, session_id: str, callback: Callable[[str], None]) -> None:
        """Register a callback for terminal output from a session."""
        self._output_callbacks[session_id] = callback

    def on_exit(self, session_id: str, callback: Callable[[int], None]) -> None:
        """Register a callback for session exit."""
        self._exit_callbacks[session_id] = callback

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _wait_connected(self, timeout: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError("Not connected to remote agent")

    async def _authenticate(self) -> bool:
        """Perform HMAC challenge-response authentication."""
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)  # type: ignore
            msg = Message.from_json(raw)
        except Exception as e:
            logger.error("Auth handshake failed: %s", e)
            return False

        if msg.type == MessageType.AUTH_RESULT:
            result = parse_payload(msg, AuthResult)
            logger.error("Auth rejected: %s", result.message)
            return False

        if msg.type != MessageType.AUTH_CHALLENGE:
            logger.error("Expected auth_challenge, got %s", msg.type)
            return False

        challenge = parse_payload(msg, AuthChallenge)
        signature = compute_hmac(self.secret_key, challenge.nonce)

        response = make_message(
            MessageType.AUTH_RESPONSE,
            AuthResponse(
                hmac_signature=signature,
                client_version=__version__,
                hostname=socket.gethostname(),
            ),
        )
        await self._ws.send(response.to_json())  # type: ignore

        # Wait for result
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)  # type: ignore
            msg = Message.from_json(raw)
        except Exception as e:
            logger.error("Auth result failed: %s", e)
            return False

        if msg.type != MessageType.AUTH_RESULT:
            return False

        result = parse_payload(msg, AuthResult)
        return result.success

    async def _read_loop(self) -> None:
        """Read messages from the WebSocket and dispatch them."""
        try:
            async for raw in self._ws:  # type: ignore
                try:
                    msg = Message.from_json(raw)
                    await self._dispatch(msg)
                except Exception as e:
                    logger.error("Error dispatching message: %s", e)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed")
        except asyncio.CancelledError:
            return
        finally:
            self._connected.clear()
            if not self._closed and self.auto_reconnect:
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        config = load_config()
        delay = config.get("bridge", {}).get("reconnect_delay", 2)
        max_delay = config.get("bridge", {}).get("max_reconnect_delay", 60)

        while not self._closed:
            logger.info("Reconnecting in %ds...", delay)
            await asyncio.sleep(delay)
            try:
                await self.connect()
                logger.info("Reconnected!")
                return
            except Exception as e:
                logger.warning("Reconnect failed: %s", e)
                delay = min(delay * 2, max_delay)

    async def _dispatch(self, msg: Message) -> None:
        """Dispatch an incoming message."""
        # Check if it's a response to a pending request
        if msg.id in self._pending_responses:
            future = self._pending_responses.pop(msg.id)
            if not future.done():
                future.set_result(msg)
            return

        # Handle async messages
        if msg.type == MessageType.TERMINAL_OUTPUT:
            output = parse_payload(msg, TerminalOutput)
            cb = self._output_callbacks.get(output.session_id)
            if cb:
                cb(output.data)

        elif msg.type == MessageType.TERMINAL_EXIT:
            from terminal_bridge.protocol.messages import TerminalExit

            exit_info = parse_payload(msg, TerminalExit)
            cb = self._exit_callbacks.get(exit_info.session_id)
            if cb:
                cb(exit_info.exit_code)

        elif msg.type == MessageType.HEARTBEAT:
            pass  # Just a keep-alive

        elif msg.type == MessageType.ERROR:
            logger.error("Remote error: %s", msg.payload)


class RemoteTerminal:
    """Public Python SDK for Terminal Bridge.

    Usage:
        remote = RemoteTerminal("192.168.1.50")
        await remote.connect()
        result = await remote.exec("ls -la")
        print(result["stdout"])
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_AGENT_PORT,
        secret_key: str | None = None,
        use_tls: bool = True,
        name: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self._secret_key = secret_key
        self._use_tls = use_tls
        self._conn: ConnectionManager | None = None

    async def connect(self) -> None:
        """Connect to the remote agent."""
        # Resolve secret key
        secret_key = self._secret_key
        if secret_key is None:
            # Try loading from config by name or host
            if self.name:
                remote_cfg = get_remote_config(self.name)
                if remote_cfg:
                    secret_key = retrieve_secret(f"remote_{self.name}")
                    self.host = remote_cfg.get("host", self.host)
                    self.port = remote_cfg.get("port", self.port)
            if secret_key is None:
                secret_key = retrieve_secret("remote_default")
            if secret_key is None:
                raise ValueError(
                    "No secret key found. Run 'tbridge setup local <PAIRING_CODE>' first."
                )

        scheme = "wss" if self._use_tls else "ws"
        url = f"{scheme}://{self.host}:{self.port}"

        self._conn = ConnectionManager(
            url=url,
            secret_key=secret_key,
            use_tls=self._use_tls,
        )
        await self._conn.connect()

    async def disconnect(self) -> None:
        """Disconnect from the remote agent."""
        if self._conn:
            await self._conn.disconnect()

    async def exec(
        self,
        command: str,
        timeout: float = 30.0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a command on the remote Mac.

        Returns dict with keys: stdout, stderr, exit_code, duration, timed_out
        """
        msg = make_message(
            MessageType.EXEC_REQUEST,
            ExecRequest(
                command=command,
                timeout=timeout,
                cwd=cwd,
                env=env or {},
                session_id=session_id,
            ),
        )
        reply = await self._conn.send_and_wait(msg, timeout=timeout + 5)  # type: ignore
        result = parse_payload(reply, ExecResult)
        return result.model_dump()

    async def create_session(
        self,
        session_id: str | None = None,
        shell: str | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> str:
        """Create an interactive PTY session. Returns session_id."""
        import uuid

        if session_id is None:
            session_id = uuid.uuid4().hex[:8]

        msg = make_message(
            MessageType.SESSION_CREATE,
            SessionCreate(session_id=session_id, shell=shell, cols=cols, rows=rows),
        )
        reply = await self._conn.send_and_wait(msg)  # type: ignore
        created = parse_payload(reply, SessionCreated)
        return created.session_id

    async def destroy_session(self, session_id: str) -> int | None:
        """Destroy a PTY session. Returns exit code."""
        msg = make_message(
            MessageType.SESSION_DESTROY,
            SessionDestroy(session_id=session_id),
        )
        reply = await self._conn.send_and_wait(msg)  # type: ignore
        destroyed = parse_payload(reply, SessionDestroyed)
        return destroyed.exit_code

    async def send_input(self, session_id: str, data: str) -> None:
        """Send input (text) to a PTY session."""
        encoded = base64.b64encode(data.encode()).decode("ascii")
        msg = make_message(
            MessageType.TERMINAL_INPUT,
            TerminalInput(session_id=session_id, data=encoded),
        )
        await self._conn.send_fire_forget(msg)  # type: ignore

    async def read_output(
        self,
        session_id: str,
        timeout: float = 5.0,
    ) -> str:
        """Read output from a PTY session (blocks until data or timeout)."""
        output_parts: list[str] = []
        got_data = asyncio.Event()

        def on_data(data: str) -> None:
            output_parts.append(base64.b64decode(data).decode("utf-8", errors="replace"))
            got_data.set()

        self._conn.on_output(session_id, on_data)  # type: ignore
        try:
            await asyncio.wait_for(got_data.wait(), timeout=timeout)
            # Give a bit more time for additional output
            await asyncio.sleep(0.1)
        except asyncio.TimeoutError:
            pass
        finally:
            self._conn._output_callbacks.pop(session_id, None)  # type: ignore

        return "".join(output_parts)

    async def resize_session(self, session_id: str, cols: int, rows: int) -> None:
        """Resize a PTY session."""
        msg = make_message(
            MessageType.TERMINAL_RESIZE,
            TerminalResize(session_id=session_id, cols=cols, rows=rows),
        )
        await self._conn.send_fire_forget(msg)  # type: ignore

    async def list_sessions(self) -> list[dict]:
        """List active PTY sessions on the remote."""
        msg = make_message(MessageType.SESSION_LIST)
        reply = await self._conn.send_and_wait(msg)  # type: ignore
        result = parse_payload(reply, SessionListResult)
        return [s.model_dump() for s in result.sessions]

    async def push_file(self, local_path: str, remote_path: str) -> None:
        """Upload a file to the remote Mac."""
        content = Path(local_path).read_bytes()
        msg = make_message(
            MessageType.FILE_PUSH,
            FilePush(
                path=remote_path,
                content=base64.b64encode(content).decode("ascii"),
            ),
        )
        reply = await self._conn.send_and_wait(msg, timeout=60)  # type: ignore
        result = parse_payload(reply, FileResult)
        if not result.success:
            raise RuntimeError(f"File push failed: {result.message}")

    async def pull_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the remote Mac."""
        msg = make_message(
            MessageType.FILE_PULL_REQUEST,
            FilePullRequest(path=remote_path),
        )
        reply = await self._conn.send_and_wait(msg, timeout=60)  # type: ignore
        if reply.type == MessageType.ERROR:
            raise RuntimeError(f"File pull failed: {reply.payload}")
        result = parse_payload(reply, FilePullResult)
        content = base64.b64decode(result.content)
        Path(local_path).write_bytes(content)

    async def system_info(self) -> dict:
        """Get system information about the remote Mac."""
        msg = make_message(MessageType.SYSTEM_INFO_REQUEST)
        reply = await self._conn.send_and_wait(msg)  # type: ignore
        from terminal_bridge.protocol.messages import SystemInfoResult

        result = parse_payload(reply, SystemInfoResult)
        return result.model_dump()

    async def ping(self) -> float:
        """Ping the remote and return latency in milliseconds."""
        msg = make_message(MessageType.PING, Ping())
        start = time.time()
        reply = await self._conn.send_and_wait(msg, timeout=5)  # type: ignore
        latency = (time.time() - start) * 1000
        return round(latency, 1)

    # Context manager support
    async def __aenter__(self) -> RemoteTerminal:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()


# ---------------------------------------------------------------------------
# Functions used by the CLI (thin wrappers around RemoteTerminal)
# ---------------------------------------------------------------------------

async def _get_remote_terminal(remote: str | None = None) -> RemoteTerminal:
    """Get a connected RemoteTerminal from config or args."""
    config = load_config()

    if remote:
        # Try as a named remote
        remote_cfg = get_remote_config(remote)
        if remote_cfg:
            rt = RemoteTerminal(
                host=remote_cfg["host"],
                port=remote_cfg.get("port", DEFAULT_AGENT_PORT),
                name=remote,
            )
            await rt.connect()
            return rt
        # Try as host:port
        if ":" in remote:
            host, port_str = remote.rsplit(":", 1)
            rt = RemoteTerminal(host=host, port=int(port_str))
            await rt.connect()
            return rt
        # Try as just a host
        rt = RemoteTerminal(host=remote)
        await rt.connect()
        return rt

    # Use default remote
    remotes = config.get("remotes", {})
    if not remotes:
        raise click.ClickException(
            "No remote configured. Run 'tbridge setup local <PAIRING_CODE>' first."
        )

    # Use first remote as default
    name = next(iter(remotes))
    remote_cfg = remotes[name]
    rt = RemoteTerminal(
        host=remote_cfg["host"],
        port=remote_cfg.get("port", DEFAULT_AGENT_PORT),
        name=name,
    )
    await rt.connect()
    return rt


async def exec_remote_command(
    command: str,
    remote: str | None = None,
    session: str | None = None,
    timeout: int = 30,
) -> dict:
    """Execute a command on the remote Mac (used by CLI)."""
    rt = await _get_remote_terminal(remote)
    try:
        return await rt.exec(command, timeout=timeout, session_id=session)
    finally:
        await rt.disconnect()


async def list_remote_sessions(remote: str | None = None) -> list[dict]:
    """List sessions on the remote (used by CLI)."""
    rt = await _get_remote_terminal(remote)
    try:
        return await rt.list_sessions()
    finally:
        await rt.disconnect()


async def push_file(
    local_path: str,
    remote_path: str,
    remote: str | None = None,
) -> None:
    """Push a file to the remote (used by CLI)."""
    rt = await _get_remote_terminal(remote)
    try:
        await rt.push_file(local_path, remote_path)
    finally:
        await rt.disconnect()


async def pull_file(
    remote_path: str,
    local_path: str,
    remote: str | None = None,
) -> None:
    """Pull a file from the remote (used by CLI)."""
    rt = await _get_remote_terminal(remote)
    try:
        await rt.pull_file(remote_path, local_path)
    finally:
        await rt.disconnect()


async def get_status(remote: str | None = None) -> dict:
    """Get connection status (used by CLI)."""
    rt = await _get_remote_terminal(remote)
    try:
        latency = await rt.ping()
        info = await rt.system_info()
        sessions = await rt.list_sessions()
        return {
            "mode": "direct" if "relay" not in rt.host else "relay",
            "hostname": info.get("hostname", "unknown"),
            "latency_ms": latency,
            "active_sessions": len(sessions),
            "uptime": info.get("uptime", "unknown"),
        }
    finally:
        await rt.disconnect()


async def connect_to_remote(
    remote: str,
    name: str | None = None,
    interactive: bool = False,
) -> None:
    """Connect to a remote and optionally open interactive terminal."""
    from rich.console import Console

    console = Console()
    rt = await _get_remote_terminal(remote)
    try:
        latency = await rt.ping()
        info = await rt.system_info()
        console.print(f"[green]Connected to {info['hostname']}[/green] ({latency}ms)")

        if interactive:
            from terminal_bridge.local_bridge.virtual_term import run_virtual_terminal

            await run_virtual_terminal(rt)
    finally:
        await rt.disconnect()

