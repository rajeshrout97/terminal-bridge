"""Stdio JSON pipe for Terminal Bridge.

Reads JSON commands from stdin, executes them on the remote Mac,
and writes JSON results to stdout. Designed for tool-use integration
with Claude CLI, aider, and other AI tools that support function calling.

Usage:
    echo '{"tool": "exec", "command": "ls -la"}' | tbridge pipe
    echo '{"tool": "system_info"}' | tbridge pipe

Supported tools:
    exec           - Execute a command
    session_start  - Start a persistent session
    session_input  - Send input to a session
    session_output - Read session output
    session_end    - End a session
    sessions       - List sessions
    file_push      - Upload a file (content as base64)
    file_pull      - Download a file
    system_info    - Get remote system info
    ping           - Ping remote
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from typing import Any

from terminal_bridge.local_bridge.client import RemoteTerminal, _get_remote_terminal

logger = logging.getLogger(__name__)

_remote: RemoteTerminal | None = None
_session_outputs: dict[str, list[str]] = {}


def _write_response(data: dict) -> None:
    """Write a JSON response to stdout."""
    json.dump(data, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _write_error(message: str) -> None:
    """Write an error response to stdout."""
    _write_response({"error": message})


async def _handle_request(request: dict) -> dict:
    """Handle a single JSON request."""
    global _remote

    tool = request.get("tool", "")

    if _remote is None:
        return {"error": "Not connected to remote"}

    try:
        if tool == "exec":
            result = await _remote.exec(
                command=request.get("command", ""),
                timeout=request.get("timeout", 30),
                cwd=request.get("cwd"),
                env=request.get("env"),
                session_id=request.get("session_id"),
            )
            return result

        elif tool == "session_start":
            sid = await _remote.create_session(
                session_id=request.get("session_id"),
                shell=request.get("shell"),
            )
            _session_outputs[sid] = []

            def on_output(data: str) -> None:
                decoded = base64.b64decode(data).decode("utf-8", errors="replace")
                _session_outputs.setdefault(sid, []).append(decoded)

            _remote._conn.on_output(sid, on_output)  # type: ignore
            return {"session_id": sid}

        elif tool == "session_input":
            session_id = request.get("session_id", "")
            input_text = request.get("input", "")
            wait = request.get("wait", 2.0)

            _session_outputs.setdefault(session_id, []).clear()
            await _remote.send_input(session_id, input_text)
            await asyncio.sleep(wait)

            output = "".join(_session_outputs.get(session_id, []))
            _session_outputs.setdefault(session_id, []).clear()
            return {"session_id": session_id, "output": output}

        elif tool == "session_output":
            session_id = request.get("session_id", "")
            wait = request.get("wait", 1.0)
            await asyncio.sleep(wait)
            output = "".join(_session_outputs.get(session_id, []))
            _session_outputs.setdefault(session_id, []).clear()
            return {"session_id": session_id, "output": output}

        elif tool == "session_end":
            session_id = request.get("session_id", "")
            exit_code = await _remote.destroy_session(session_id)
            _session_outputs.pop(session_id, None)
            return {"session_id": session_id, "exit_code": exit_code}

        elif tool == "sessions":
            sessions = await _remote.list_sessions()
            return {"sessions": sessions}

        elif tool == "file_push":
            path = request.get("path", "")
            content = request.get("content", "")  # base64
            from terminal_bridge.protocol.messages import (
                FilePush,
                FileResult,
                MessageType,
                make_message,
                parse_payload,
            )

            msg = make_message(
                MessageType.FILE_PUSH,
                FilePush(path=path, content=content),
            )
            reply = await _remote._conn.send_and_wait(msg, timeout=60)  # type: ignore
            result = parse_payload(reply, FileResult)
            return {"success": result.success, "message": result.message}

        elif tool == "file_pull":
            path = request.get("path", "")
            from terminal_bridge.protocol.messages import (
                FilePullRequest,
                FilePullResult,
                MessageType,
                make_message,
                parse_payload,
            )

            msg = make_message(
                MessageType.FILE_PULL_REQUEST,
                FilePullRequest(path=path),
            )
            reply = await _remote._conn.send_and_wait(msg, timeout=60)  # type: ignore
            if reply.type == MessageType.ERROR:
                return {"error": str(reply.payload)}
            result = parse_payload(reply, FilePullResult)
            return {
                "path": result.path,
                "content": result.content,
                "size": result.size,
            }

        elif tool == "system_info":
            info = await _remote.system_info()
            return info

        elif tool == "ping":
            latency = await _remote.ping()
            return {"latency_ms": latency}

        else:
            return {"error": f"Unknown tool: {tool}"}

    except Exception as e:
        return {"error": str(e)}


async def run_stdio_pipe(remote: str | None = None) -> None:
    """Run the stdio JSON pipe mode.

    Reads JSON lines from stdin, processes them, writes JSON to stdout.
    """
    global _remote

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Connect to remote
    try:
        _remote = await _get_remote_terminal(remote)
        print(
            json.dumps({"status": "connected", "message": "Terminal Bridge ready"}),
            file=sys.stderr,
        )
    except Exception as e:
        _write_error(f"Failed to connect: {e}")
        sys.exit(1)

    # Read from stdin line by line
    loop = asyncio.get_event_loop()

    try:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                request = json.loads(line_str)
            except json.JSONDecodeError as e:
                _write_error(f"Invalid JSON: {e}")
                continue

            result = await _handle_request(request)
            _write_response(result)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if _remote:
            await _remote.disconnect()

