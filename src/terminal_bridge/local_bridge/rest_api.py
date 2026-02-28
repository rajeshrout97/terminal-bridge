"""REST API server for Terminal Bridge.

Runs on localhost:9876 and provides an HTTP API that any AI tool
(Ollama, LM Studio, custom scripts) can use to execute commands
on the remote Mac.

Endpoints:
    POST /api/exec              - Execute a command
    POST /api/session/start     - Create a PTY session
    POST /api/session/{id}/input - Send input to session
    GET  /api/session/{id}/output - Read session output
    DELETE /api/session/{id}     - Destroy a session
    GET  /api/sessions           - List sessions
    POST /api/file/push          - Upload a file
    POST /api/file/pull          - Download a file
    GET  /api/system-info        - Get remote system info
    GET  /api/ping               - Ping remote
    GET  /api/health             - Health check
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

from aiohttp import web

from terminal_bridge.config import DEFAULT_API_PORT
from terminal_bridge.local_bridge.client import RemoteTerminal, _get_remote_terminal

logger = logging.getLogger(__name__)

# Shared state for the API server
_remote: RemoteTerminal | None = None
_session_outputs: dict[str, list[str]] = {}  # session_id -> output buffer


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error_response(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _ensure_connected() -> RemoteTerminal:
    global _remote
    if _remote is None or not _remote._conn or not _remote._conn.connected:
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "Not connected to remote. Run 'tbridge connect' first."}),
            content_type="application/json",
        )
    return _remote


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_exec(request: web.Request) -> web.Response:
    """POST /api/exec - Execute a command on the remote Mac."""
    rt = await _ensure_connected()
    try:
        body = await request.json()
    except Exception:
        return _error_response("Invalid JSON body")

    command = body.get("command")
    if not command:
        return _error_response("'command' is required")

    timeout = body.get("timeout", 30)
    cwd = body.get("cwd")
    env = body.get("env", {})
    session_id = body.get("session_id")

    try:
        result = await rt.exec(
            command=command,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
        return _json_response(result)
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_session_start(request: web.Request) -> web.Response:
    """POST /api/session/start - Create a new PTY session."""
    rt = await _ensure_connected()
    try:
        body = await request.json()
    except Exception:
        body = {}

    shell = body.get("shell")
    cols = body.get("cols", 80)
    rows = body.get("rows", 24)
    session_id = body.get("session_id")

    try:
        sid = await rt.create_session(
            session_id=session_id,
            shell=shell,
            cols=cols,
            rows=rows,
        )

        # Set up output buffer for this session
        _session_outputs[sid] = []

        def on_output(data: str) -> None:
            decoded = base64.b64decode(data).decode("utf-8", errors="replace")
            _session_outputs.setdefault(sid, []).append(decoded)

        rt._conn.on_output(sid, on_output)  # type: ignore

        return _json_response({"session_id": sid})
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_session_input(request: web.Request) -> web.Response:
    """POST /api/session/{id}/input - Send input to a session."""
    rt = await _ensure_connected()
    session_id = request.match_info["id"]

    try:
        body = await request.json()
    except Exception:
        return _error_response("Invalid JSON body")

    input_text = body.get("input", "")
    try:
        await rt.send_input(session_id, input_text)
        return _json_response({"status": "sent"})
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_session_output(request: web.Request) -> web.Response:
    """GET /api/session/{id}/output - Read buffered session output."""
    await _ensure_connected()
    session_id = request.match_info["id"]

    # Wait briefly for output
    timeout = float(request.query.get("timeout", "2"))
    end_time = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < end_time:
        if _session_outputs.get(session_id):
            break
        await asyncio.sleep(0.1)

    output = _session_outputs.get(session_id, [])
    _session_outputs[session_id] = []  # Clear buffer

    return _json_response({
        "session_id": session_id,
        "output": "".join(output),
    })


async def handle_session_destroy(request: web.Request) -> web.Response:
    """DELETE /api/session/{id} - Destroy a session."""
    rt = await _ensure_connected()
    session_id = request.match_info["id"]

    try:
        exit_code = await rt.destroy_session(session_id)
        _session_outputs.pop(session_id, None)
        return _json_response({"session_id": session_id, "exit_code": exit_code})
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_sessions_list(request: web.Request) -> web.Response:
    """GET /api/sessions - List all active sessions."""
    rt = await _ensure_connected()
    try:
        sessions = await rt.list_sessions()
        return _json_response({"sessions": sessions})
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_file_push(request: web.Request) -> web.Response:
    """POST /api/file/push - Upload a file to the remote."""
    rt = await _ensure_connected()
    try:
        body = await request.json()
    except Exception:
        return _error_response("Invalid JSON body")

    remote_path = body.get("path")
    content = body.get("content")  # base64 encoded
    local_path = body.get("local_path")  # alternative: read from local file

    if not remote_path:
        return _error_response("'path' is required")

    if local_path:
        try:
            await rt.push_file(local_path, remote_path)
            return _json_response({"status": "pushed", "path": remote_path})
        except Exception as e:
            return _error_response(str(e), status=500)
    elif content:
        # Direct content push via the exec protocol
        from terminal_bridge.protocol.messages import (
            FilePush,
            FileResult,
            MessageType,
            make_message,
            parse_payload,
        )

        msg = make_message(
            MessageType.FILE_PUSH,
            FilePush(path=remote_path, content=content),
        )
        reply = await rt._conn.send_and_wait(msg, timeout=60)  # type: ignore
        result = parse_payload(reply, FileResult)
        if result.success:
            return _json_response({"status": "pushed", "path": remote_path})
        else:
            return _error_response(result.message, status=500)
    else:
        return _error_response("'content' (base64) or 'local_path' is required")


async def handle_file_pull(request: web.Request) -> web.Response:
    """POST /api/file/pull - Download a file from the remote."""
    rt = await _ensure_connected()

    # Support both GET query params and POST JSON body
    if request.method == "POST":
        try:
            body = await request.json()
            remote_path = body.get("path")
        except Exception:
            return _error_response("Invalid JSON body")
    else:
        remote_path = request.query.get("path")

    if not remote_path:
        return _error_response("'path' is required")

    try:
        from terminal_bridge.protocol.messages import (
            FilePullRequest,
            FilePullResult,
            MessageType,
            make_message,
            parse_payload,
        )

        msg = make_message(
            MessageType.FILE_PULL_REQUEST,
            FilePullRequest(path=remote_path),
        )
        reply = await rt._conn.send_and_wait(msg, timeout=60)  # type: ignore
        if reply.type == MessageType.ERROR:
            return _error_response(str(reply.payload), status=404)
        result = parse_payload(reply, FilePullResult)
        return _json_response({
            "path": result.path,
            "content": result.content,
            "size": result.size,
        })
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_system_info(request: web.Request) -> web.Response:
    """GET /api/system-info - Get remote system information."""
    rt = await _ensure_connected()
    try:
        info = await rt.system_info()
        return _json_response(info)
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_ping(request: web.Request) -> web.Response:
    """GET /api/ping - Ping the remote."""
    rt = await _ensure_connected()
    try:
        latency = await rt.ping()
        return _json_response({"latency_ms": latency})
    except Exception as e:
        return _error_response(str(e), status=500)


async def handle_health(request: web.Request) -> web.Response:
    """GET /api/health - Health check."""
    connected = _remote is not None and _remote._conn is not None and _remote._conn.connected
    return _json_response({
        "status": "ok" if connected else "disconnected",
        "connected": connected,
    })


# ---------------------------------------------------------------------------
# CORS middleware (allow localhost origins for browser-based tools)
# ---------------------------------------------------------------------------

@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ---------------------------------------------------------------------------
# App factory and entry point
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_post("/api/exec", handle_exec)
    app.router.add_post("/api/session/start", handle_session_start)
    app.router.add_post("/api/session/{id}/input", handle_session_input)
    app.router.add_get("/api/session/{id}/output", handle_session_output)
    app.router.add_delete("/api/session/{id}", handle_session_destroy)
    app.router.add_get("/api/sessions", handle_sessions_list)
    app.router.add_post("/api/file/push", handle_file_push)
    app.router.add_post("/api/file/pull", handle_file_pull)
    app.router.add_get("/api/file/pull", handle_file_pull)
    app.router.add_get("/api/system-info", handle_system_info)
    app.router.add_get("/api/ping", handle_ping)
    app.router.add_get("/api/health", handle_health)

    return app


async def run_rest_api(remote: str | None = None, port: int = DEFAULT_API_PORT) -> None:
    """Start the REST API server."""
    global _remote

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from rich.console import Console

    console = Console()

    # Connect to remote
    try:
        _remote = await _get_remote_terminal(remote)
        latency = await _remote.ping()
        info = await _remote.system_info()
        console.print(
            f"[green]Connected to {info['hostname']}[/green] ({latency}ms)"
        )
    except Exception as e:
        console.print(f"[red]Failed to connect to remote: {e}[/red]")
        console.print("[yellow]Starting API in disconnected mode. Connect later.[/yellow]")

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    console.print(f"\n[bold green]REST API running on http://127.0.0.1:{port}[/bold green]")
    console.print(f"[dim]Endpoints: /api/exec, /api/sessions, /api/system-info, ...[/dim]")
    console.print("Press Ctrl+C to stop.\n")

    # Wait for shutdown
    stop_event = asyncio.Event()

    import signal

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    await runner.cleanup()
    if _remote:
        await _remote.disconnect()

