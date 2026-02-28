"""MCP Server for Terminal Bridge -- Cursor integration.

Exposes remote terminal tools via the Model Context Protocol (MCP),
allowing Cursor's AI agent to natively execute commands, manage
sessions, and transfer files on a remote Mac.

Usage:
    tbridge mcp --remote <name>

Cursor config (~/.cursor/mcp.json):
    {
        "mcpServers": {
            "terminal-bridge": {
                "command": "tbridge",
                "args": ["mcp"]
            }
        }
    }
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_remote = None
_remote_name: str | None = None
_session_outputs: dict[str, list[str]] = {}


async def _ensure_remote():
    """Lazily connect to the remote on first tool use."""
    global _remote
    if _remote is not None and _remote._conn and _remote._conn.connected:
        return _remote

    from terminal_bridge.local_bridge.client import _get_remote_terminal

    _remote = await _get_remote_terminal(_remote_name)
    return _remote


def run_mcp_server(remote: str | None = None) -> None:
    """Start the MCP server (stdio transport for Cursor).

    This is called synchronously from the CLI. FastMCP.run() manages
    its own event loop, so we must NOT use asyncio.run() here.
    """
    global _remote_name
    _remote_name = remote

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "MCP SDK not installed. Install with: pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp = FastMCP(
        "terminal-bridge",
        instructions=(
            "Terminal Bridge provides remote terminal access to another Mac. "
            "Use execute_command for one-shot commands. Use session tools for "
            "interactive workflows that need state (cd, env vars, etc.)."
        ),
    )

    # -----------------------------------------------------------------
    # Tool definitions — connection is lazy (on first call)
    # -----------------------------------------------------------------

    @mcp.tool()
    async def execute_command(
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> str:
        """Execute a command on the remote Mac and return the output.

        Args:
            command: The shell command to execute (e.g. "ls -la", "brew install ffmpeg")
            timeout: Maximum seconds to wait for the command (default 30)
            cwd: Working directory for the command (optional)

        Returns:
            Command output (stdout + stderr) and exit code
        """
        rt = await _ensure_remote()
        result = await rt.exec(command=command, timeout=timeout, cwd=cwd)

        parts = []
        if result["stdout"]:
            parts.append(result["stdout"])
        if result["stderr"]:
            parts.append(f"[stderr] {result['stderr']}")
        parts.append(f"\n[exit_code: {result['exit_code']}] [duration: {result['duration']}s]")
        if result["timed_out"]:
            parts.append("[TIMED OUT]")

        return "\n".join(parts)

    @mcp.tool()
    async def start_session(
        session_name: str = "default",
        shell: str | None = None,
    ) -> str:
        """Start a persistent terminal session on the remote Mac.

        Sessions maintain state (working directory, environment variables, etc.)
        across multiple commands. Use send_to_session to send commands.

        Args:
            session_name: A name for this session (default: "default")
            shell: Shell to use (default: remote Mac's default shell)

        Returns:
            Session ID for use with other session commands
        """
        rt = await _ensure_remote()
        sid = await rt.create_session(session_id=session_name, shell=shell)
        _session_outputs[sid] = []

        def on_output(data: str) -> None:
            decoded = base64.b64decode(data).decode("utf-8", errors="replace")
            _session_outputs.setdefault(sid, []).append(decoded)

        rt._conn.on_output(sid, on_output)
        return f"Session '{sid}' started."

    @mcp.tool()
    async def send_to_session(
        session_name: str,
        input_text: str,
        wait_seconds: float = 2.0,
    ) -> str:
        """Send input to a persistent session and read the output.

        Args:
            session_name: The session ID/name to send to
            input_text: Text to send (e.g. "cd /project\\nmake build\\n")
            wait_seconds: How long to wait for output (default 2.0)

        Returns:
            Output produced by the command
        """
        rt = await _ensure_remote()
        _session_outputs.setdefault(session_name, [])
        _session_outputs[session_name].clear()

        await rt.send_input(session_name, input_text)
        await asyncio.sleep(wait_seconds)

        output = "".join(_session_outputs.get(session_name, []))
        _session_outputs[session_name].clear()
        return output if output else "(no output)"

    @mcp.tool()
    async def read_session_output(
        session_name: str,
        wait_seconds: float = 1.0,
    ) -> str:
        """Read any pending output from a session without sending input.

        Args:
            session_name: The session to read from
            wait_seconds: How long to wait for output

        Returns:
            Buffered output from the session
        """
        await asyncio.sleep(wait_seconds)
        output = "".join(_session_outputs.get(session_name, []))
        _session_outputs.setdefault(session_name, []).clear()
        return output if output else "(no output)"

    @mcp.tool()
    async def end_session(session_name: str) -> str:
        """End a persistent terminal session.

        Args:
            session_name: The session to destroy

        Returns:
            Confirmation with exit code
        """
        rt = await _ensure_remote()
        exit_code = await rt.destroy_session(session_name)
        _session_outputs.pop(session_name, None)
        return f"Session '{session_name}' ended (exit code: {exit_code})"

    @mcp.tool()
    async def list_sessions() -> str:
        """List all active terminal sessions on the remote Mac.

        Returns:
            Table of active sessions with IDs, shells, and status
        """
        rt = await _ensure_remote()
        sessions = await rt.list_sessions()
        if not sessions:
            return "No active sessions."

        lines = ["Active sessions:"]
        for s in sessions:
            lines.append(
                f"  - {s['id']}: shell={s['shell']}, "
                f"started={s['started']}, idle={s['idle']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def upload_file(remote_path: str, content: str) -> str:
        """Upload a text file to the remote Mac.

        Args:
            remote_path: Destination path on the remote Mac
            content: File content (text)

        Returns:
            Confirmation message
        """
        rt = await _ensure_remote()
        from terminal_bridge.protocol.messages import (
            FilePush,
            FileResult,
            MessageType,
            make_message,
            parse_payload,
        )

        encoded = base64.b64encode(content.encode()).decode("ascii")
        msg = make_message(
            MessageType.FILE_PUSH,
            FilePush(path=remote_path, content=encoded),
        )
        reply = await rt._conn.send_and_wait(msg, timeout=30)
        result = parse_payload(reply, FileResult)
        return result.message if result.success else f"Failed: {result.message}"

    @mcp.tool()
    async def download_file(remote_path: str) -> str:
        """Download a text file from the remote Mac.

        Args:
            remote_path: Path of the file on the remote Mac

        Returns:
            File content as text
        """
        rt = await _ensure_remote()
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
        reply = await rt._conn.send_and_wait(msg, timeout=30)
        if reply.type == MessageType.ERROR:
            return f"Error: {reply.payload}"
        result = parse_payload(reply, FilePullResult)
        return base64.b64decode(result.content).decode("utf-8", errors="replace")

    @mcp.tool()
    async def get_system_info() -> str:
        """Get system information about the remote Mac.

        Returns:
            Hostname, OS, CPU, memory, disk, and other system details
        """
        rt = await _ensure_remote()
        info = await rt.system_info()
        lines = [
            f"Hostname: {info['hostname']}",
            f"OS: {info['os_version']}",
            f"Architecture: {info['architecture']}",
            f"CPUs: {info['cpu_count']}",
            f"Memory: {info['memory_available_gb']}GB / {info['memory_total_gb']}GB",
            f"Disk: {info['disk_free_gb']}GB free / {info['disk_total_gb']}GB",
            f"Shell: {info['shell']}",
            f"User: {info['username']}",
            f"Agent Uptime: {info['uptime']}",
        ]
        return "\n".join(lines)

    # Run MCP server on stdio — this blocks and manages its own event loop
    mcp.run(transport="stdio")
