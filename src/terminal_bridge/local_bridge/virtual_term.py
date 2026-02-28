"""Virtual terminal proxy for Terminal Bridge.

Creates a local interactive terminal that transparently proxies
all I/O to a remote Mac's PTY session. Feels like a local terminal
but commands execute on the remote machine.

Usage:
    tbridge connect <remote> --terminal
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import struct
import sys
import termios
import tty
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terminal_bridge.local_bridge.client import RemoteTerminal


async def run_virtual_terminal(remote: "RemoteTerminal") -> None:
    """Run an interactive virtual terminal connected to the remote Mac.

    Takes over the current terminal and proxies all I/O to the remote.
    """
    from rich.console import Console

    console = Console(stderr=True)

    # Get current terminal size
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 80, 24

    # Create a session on the remote
    session_id = await remote.create_session(cols=cols, rows=rows)
    console.print(
        f"[dim]Connected to remote session {session_id}. Press Ctrl+] to disconnect.[/dim]",
        file=sys.stderr,
    )

    # Save original terminal settings
    if sys.stdin.isatty():
        old_settings = termios.tcgetattr(sys.stdin.fileno())
    else:
        console.print("[red]Not a terminal. Use --terminal with an interactive shell.[/red]")
        return

    exit_code = 0
    session_exited = asyncio.Event()

    try:
        # Put terminal in raw mode
        tty.setraw(sys.stdin.fileno())

        # Set up output callback: write remote output to local terminal
        def on_output(data: str) -> None:
            raw = base64.b64decode(data)
            os.write(sys.stdout.fileno(), raw)

        def on_exit(code: int) -> None:
            nonlocal exit_code
            exit_code = code
            session_exited.set()

        remote._conn.on_output(session_id, on_output)  # type: ignore
        remote._conn.on_exit(session_id, on_exit)  # type: ignore

        # Set up SIGWINCH handler for terminal resize
        def handle_resize(signum: int, frame: object) -> None:
            try:
                new_cols, new_rows = os.get_terminal_size()
                asyncio.get_event_loop().create_task(
                    remote.resize_session(session_id, new_cols, new_rows)
                )
            except Exception:
                pass

        old_sigwinch = signal.signal(signal.SIGWINCH, handle_resize)

        # Read local input and send to remote
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # Input reading task
        async def read_input() -> None:
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break

                    # Check for escape sequence: Ctrl+] (0x1d)
                    if b"\x1d" in data:
                        break

                    encoded = base64.b64encode(data).decode("ascii")
                    await remote.send_input(session_id, data.decode("utf-8", errors="replace"))
            except (asyncio.CancelledError, ConnectionError):
                pass

        input_task = asyncio.create_task(read_input())

        # Wait for either input task to end or session to exit
        done, pending = await asyncio.wait(
            [input_task, asyncio.create_task(session_exited.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    finally:
        # Restore terminal settings
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        signal.signal(signal.SIGWINCH, old_sigwinch)

        # Clean up session
        try:
            await remote.destroy_session(session_id)
        except Exception:
            pass

        console.print(
            f"\n[dim]Session ended (exit code: {exit_code})[/dim]",
            file=sys.stderr,
        )

