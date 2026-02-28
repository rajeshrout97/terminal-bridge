"""Remote agent WebSocket server.

Runs on the Mac you want to control. Accepts authenticated WebSocket
connections and manages PTY sessions, command execution, and file transfers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import signal
import socket
import time
from pathlib import Path

import websockets.asyncio.server

from terminal_bridge.config import DEFAULT_AGENT_PORT, load_config
from terminal_bridge.protocol.messages import (
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
    SessionInfo,
    SessionListResult,
    SystemInfoResult,
    TerminalExit,
    TerminalInput,
    TerminalOutput,
    TerminalResize,
    make_message,
    parse_payload,
)
from terminal_bridge.remote_agent.auth import authenticate_client
from terminal_bridge.remote_agent.pty_manager import PTYManager
from terminal_bridge.version import __version__

logger = logging.getLogger(__name__)


class RemoteAgentServer:
    """WebSocket server that manages remote terminal sessions."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_AGENT_PORT) -> None:
        self.host = host
        self.port = port
        self.pty_manager = PTYManager()
        self._clients: set[websockets.asyncio.server.ServerConnection] = set()
        self._client_sessions: dict[str, set[str]] = {}  # client_id -> session_ids
        self._server: websockets.asyncio.server.Server | None = None
        self._start_time = time.time()

    async def start(self) -> None:
        """Start the WebSocket server."""
        config = load_config()
        use_tls = config.get("security", {}).get("tls_enabled", True)

        ssl_context = None
        if use_tls:
            try:
                from terminal_bridge.security.tls import get_ssl_context_server
                ssl_context = get_ssl_context_server()
                logger.info("TLS enabled")
            except Exception as e:
                logger.warning("TLS setup failed (%s), running without TLS", e)

        self._server = await websockets.asyncio.server.serve(
            self._handle_client,
            self.host,
            self.port,
            ssl=ssl_context,
        )

        scheme = "wss" if ssl_context else "ws"
        logger.info(
            "Remote agent listening on %s://%s:%d",
            scheme, self.host, self.port,
        )

    async def stop(self) -> None:
        """Stop the server and clean up all sessions."""
        logger.info("Shutting down remote agent...")
        await self.pty_manager.destroy_all()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Remote agent stopped.")

    async def _handle_client(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
    ) -> None:
        """Handle a single client connection."""
        client_id = id(websocket)
        remote_addr = websocket.remote_address

        logger.info("New connection from %s", remote_addr)

        # Authenticate
        if not await authenticate_client(websocket):
            logger.warning("Authentication failed for %s", remote_addr)
            return

        self._clients.add(websocket)
        self._client_sessions[str(client_id)] = set()

        try:
            # Start heartbeat
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket))

            async for raw_message in websocket:
                try:
                    msg = Message.from_json(raw_message)
                    await self._dispatch_message(websocket, msg, str(client_id))
                except Exception as e:
                    logger.error("Error handling message: %s", e)
                    error_msg = make_message(
                        MessageType.ERROR,
                        payload_model=None,
                    )
                    error_msg.payload = {"code": "handler_error", "message": str(e)}
                    await websocket.send(error_msg.to_json())

        except websockets.exceptions.ConnectionClosed:
            logger.info("Client %s disconnected", remote_addr)
        finally:
            heartbeat_task.cancel()
            # Clean up client sessions
            session_ids = self._client_sessions.pop(str(client_id), set())
            for sid in session_ids:
                await self.pty_manager.destroy_session(sid)
            self._clients.discard(websocket)

    async def _dispatch_message(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        """Route a message to the appropriate handler."""
        handlers = {
            MessageType.SESSION_CREATE: self._handle_session_create,
            MessageType.SESSION_DESTROY: self._handle_session_destroy,
            MessageType.SESSION_LIST: self._handle_session_list,
            MessageType.TERMINAL_INPUT: self._handle_terminal_input,
            MessageType.TERMINAL_RESIZE: self._handle_terminal_resize,
            MessageType.EXEC_REQUEST: self._handle_exec_request,
            MessageType.FILE_PUSH: self._handle_file_push,
            MessageType.FILE_PULL_REQUEST: self._handle_file_pull,
            MessageType.SYSTEM_INFO_REQUEST: self._handle_system_info,
            MessageType.PING: self._handle_ping,
        }

        handler = handlers.get(msg.type)
        if handler:
            await handler(websocket, msg, client_id)
        else:
            logger.warning("Unknown message type: %s", msg.type)

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _handle_session_create(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, SessionCreate)

        def on_output(session_id: str, data: str) -> None:
            """Called when PTY produces output."""
            out_msg = make_message(
                MessageType.TERMINAL_OUTPUT,
                TerminalOutput(session_id=session_id, data=data),
            )
            asyncio.create_task(self._safe_send(websocket, out_msg))

        def on_exit(session_id: str, exit_code: int) -> None:
            """Called when PTY session exits."""
            exit_msg = make_message(
                MessageType.TERMINAL_EXIT,
                TerminalExit(session_id=session_id, exit_code=exit_code),
            )
            asyncio.create_task(self._safe_send(websocket, exit_msg))

        session = await self.pty_manager.create_session(
            session_id=req.session_id,
            shell=req.shell,
            cols=req.cols,
            rows=req.rows,
            env=req.env or None,
            cwd=req.cwd,
            output_callback=on_output,
            exit_callback=on_exit,
        )

        self._client_sessions[client_id].add(session.session_id)

        reply = make_message(
            MessageType.SESSION_CREATED,
            SessionCreated(
                session_id=session.session_id,
                shell=session.shell,
                pid=session.pid,
            ),
        )
        reply.id = msg.id  # Correlate response
        await websocket.send(reply.to_json())

    async def _handle_session_destroy(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, SessionDestroy)
        exit_code = await self.pty_manager.destroy_session(req.session_id)
        self._client_sessions.get(client_id, set()).discard(req.session_id)

        reply = make_message(
            MessageType.SESSION_DESTROYED,
            SessionDestroyed(session_id=req.session_id, exit_code=exit_code),
        )
        reply.id = msg.id
        await websocket.send(reply.to_json())

    async def _handle_session_list(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        sessions = []
        for sid, s in self.pty_manager.sessions.items():
            sessions.append(
                SessionInfo(
                    id=sid,
                    shell=s.shell,
                    pid=s.pid,
                    started=time.strftime("%H:%M:%S", time.localtime(s.started_at)),
                    idle=f"{int(s.idle_seconds)}s",
                    cols=s.cols,
                    rows=s.rows,
                )
            )

        reply = make_message(
            MessageType.SESSION_LIST_RESULT,
            SessionListResult(sessions=sessions),
        )
        reply.id = msg.id
        await websocket.send(reply.to_json())

    async def _handle_terminal_input(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, TerminalInput)
        await self.pty_manager.write_to_session(req.session_id, req.data)

    async def _handle_terminal_resize(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, TerminalResize)
        await self.pty_manager.resize_session(req.session_id, req.cols, req.rows)

    async def _handle_exec_request(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, ExecRequest)

        # If session_id is specified, write to that session instead
        if req.session_id:
            session = self.pty_manager.get_session(req.session_id)
            if session is None:
                reply = make_message(
                    MessageType.EXEC_RESULT,
                    ExecResult(
                        stdout="",
                        stderr=f"Session {req.session_id} not found",
                        exit_code=1,
                        duration=0,
                    ),
                )
                reply.id = msg.id
                await websocket.send(reply.to_json())
                return

            # Write command to session and collect output
            data_b64 = base64.b64encode(
                (req.command + "\n").encode()
            ).decode("ascii")
            await self.pty_manager.write_to_session(req.session_id, data_b64)

            # For session-based exec, we return immediately with a note
            reply = make_message(
                MessageType.EXEC_RESULT,
                ExecResult(
                    stdout=f"Command sent to session {req.session_id}",
                    stderr="",
                    exit_code=0,
                    duration=0,
                ),
            )
            reply.id = msg.id
            await websocket.send(reply.to_json())
            return

        # One-shot execution
        result = await self.pty_manager.exec_command(
            command=req.command,
            timeout=req.timeout,
            cwd=req.cwd,
            env=req.env or None,
        )

        reply = make_message(
            MessageType.EXEC_RESULT,
            ExecResult(**result),
        )
        reply.id = msg.id
        await websocket.send(reply.to_json())

    async def _handle_file_push(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, FilePush)
        try:
            path = Path(req.path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(req.content)
            path.write_bytes(content)
            path.chmod(req.mode)
            reply = make_message(
                MessageType.FILE_RESULT,
                FileResult(success=True, message=f"Written {len(content)} bytes to {req.path}"),
            )
        except Exception as e:
            reply = make_message(
                MessageType.FILE_RESULT,
                FileResult(success=False, message=str(e)),
            )
        reply.id = msg.id
        await websocket.send(reply.to_json())

    async def _handle_file_pull(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        req = parse_payload(msg, FilePullRequest)
        try:
            path = Path(req.path).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"File not found: {req.path}")
            content = path.read_bytes()
            stat = path.stat()
            reply = make_message(
                MessageType.FILE_PULL_RESULT,
                FilePullResult(
                    path=req.path,
                    content=base64.b64encode(content).decode("ascii"),
                    size=stat.st_size,
                    mode=stat.st_mode,
                ),
            )
        except Exception as e:
            reply = make_message(
                MessageType.ERROR,
                payload_model=None,
            )
            reply.payload = {"code": "file_error", "message": str(e)}
        reply.id = msg.id
        await websocket.send(reply.to_json())

    async def _handle_system_info(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        memory_total = memory_avail = disk_total = disk_free = 0.0
        try:
            import psutil

            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            memory_total = round(mem.total / (1024**3), 1)
            memory_avail = round(mem.available / (1024**3), 1)
            disk_total = round(disk.total / (1024**3), 1)
            disk_free = round(disk.free / (1024**3), 1)
        except ImportError:
            try:
                import subprocess

                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    memory_total = round(int(result.stdout.strip()) / (1024**3), 1)
                result = subprocess.run(
                    ["df", "-g", "/"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    if len(lines) >= 2:
                        parts = lines[1].split()
                        disk_total = float(parts[1])
                        disk_free = float(parts[3])
            except Exception:
                pass
        except Exception:
            pass

        uptime_secs = time.time() - self._start_time
        hours, remainder = divmod(int(uptime_secs), 3600)
        minutes, seconds = divmod(remainder, 60)

        reply = make_message(
            MessageType.SYSTEM_INFO_RESULT,
            SystemInfoResult(
                hostname=socket.gethostname(),
                os_version=platform.platform(),
                architecture=platform.machine(),
                cpu_count=os.cpu_count() or 1,
                memory_total_gb=memory_total,
                memory_available_gb=memory_avail,
                disk_total_gb=disk_total,
                disk_free_gb=disk_free,
                python_version=platform.python_version(),
                shell=os.environ.get("SHELL", "/bin/zsh"),
                username=os.environ.get("USER", "unknown"),
                uptime=f"{hours}h {minutes}m {seconds}s",
            ),
        )
        reply.id = msg.id
        await websocket.send(reply.to_json())

    async def _handle_ping(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: str,
    ) -> None:
        ping = parse_payload(msg, Ping)
        reply = make_message(
            MessageType.PONG,
            Pong(sent_at=ping.sent_at),
        )
        reply.id = msg.id
        await websocket.send(reply.to_json())

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    async def _safe_send(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
    ) -> None:
        """Send a message, ignoring errors if the connection is closed."""
        try:
            await websocket.send(msg.to_json())
        except Exception:
            pass

    async def _heartbeat_loop(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
    ) -> None:
        """Send periodic heartbeats to keep the connection alive."""
        try:
            while True:
                await asyncio.sleep(30)
                heartbeat = make_message(MessageType.HEARTBEAT)
                await self._safe_send(websocket, heartbeat)
        except asyncio.CancelledError:
            pass


async def start_agent(foreground: bool = True, port: int | None = None) -> None:
    """Start the remote agent server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()
    agent_config = config.get("agent", {})
    host = agent_config.get("host", "0.0.0.0")
    if port is None:
        port = agent_config.get("port", DEFAULT_AGENT_PORT)

    server = RemoteAgentServer(host=host, port=port)
    await server.start()

    from rich.console import Console

    console = Console()
    console.print(f"\n[bold green]Terminal Bridge Agent v{__version__}[/bold green]")
    console.print(f"Listening on [cyan]{host}:{port}[/cyan]")
    console.print(f"Hostname: [cyan]{socket.gethostname()}[/cyan]")
    console.print("Press Ctrl+C to stop.\n")

    # Wait for shutdown
    stop_event = asyncio.Event()

    def handle_signal() -> None:
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()
    await server.stop()

