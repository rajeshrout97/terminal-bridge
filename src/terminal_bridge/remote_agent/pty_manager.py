"""macOS PTY (pseudo-terminal) session manager.

Handles spawning shells, reading/writing data, resizing, and cleanup.
Uses low-level pty/os/select for efficient I/O without busy polling.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass, field
from typing import Callable

from terminal_bridge.config import load_config

logger = logging.getLogger(__name__)

# Safe environment variables to forward to PTY sessions
SAFE_ENV_VARS = {
    "TERM", "LANG", "LC_ALL", "LC_CTYPE", "HOME", "USER", "LOGNAME",
    "SHELL", "PATH", "TMPDIR", "XDG_RUNTIME_DIR",
}


@dataclass
class PTYSession:
    """Represents a single PTY session."""

    session_id: str
    pid: int
    fd: int
    shell: str
    cols: int = 80
    rows: int = 24
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    _output_callback: Callable[[str, str], None] | None = None
    _exit_callback: Callable[[str, int], None] | None = None
    _reader_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_activity

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at


class PTYManager:
    """Manages multiple PTY sessions on macOS."""

    def __init__(self) -> None:
        self._sessions: dict[str, PTYSession] = {}
        self._config = load_config()

    @property
    def sessions(self) -> dict[str, PTYSession]:
        return dict(self._sessions)

    def get_session(self, session_id: str) -> PTYSession | None:
        return self._sessions.get(session_id)

    async def create_session(
        self,
        session_id: str,
        shell: str | None = None,
        cols: int = 80,
        rows: int = 24,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        output_callback: Callable[[str, str], None] | None = None,
        exit_callback: Callable[[str, int], None] | None = None,
    ) -> PTYSession:
        """Spawn a new PTY session with the given shell."""
        if session_id in self._sessions:
            raise ValueError(f"Session {session_id} already exists")

        max_sessions = self._config.get("agent", {}).get("max_sessions", 10)
        if len(self._sessions) >= max_sessions:
            raise RuntimeError(f"Max sessions ({max_sessions}) reached")

        if shell is None:
            shell = self._config.get("agent", {}).get(
                "shell", os.environ.get("SHELL", "/bin/zsh")
            )

        # Build safe environment
        child_env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_VARS}
        child_env["TERM"] = "xterm-256color"
        child_env["TERMINAL_BRIDGE"] = "1"
        if env:
            child_env.update(env)

        # Spawn PTY
        pid, fd = pty.openpty()

        # Fork the process
        child_pid = os.fork()
        if child_pid == 0:
            # Child process
            try:
                os.close(pid)  # Close master in child
                os.setsid()  # New session

                # Set the slave as controlling terminal
                fcntl.ioctl(fd, termios.TIOCSCTTY, 0)

                # Redirect stdio
                os.dup2(fd, 0)
                os.dup2(fd, 1)
                os.dup2(fd, 2)
                if fd > 2:
                    os.close(fd)

                # Set initial window size
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)

                # Change directory
                if cwd:
                    os.chdir(cwd)

                # Exec shell
                os.execvpe(shell, [shell, "-l"], child_env)
            except Exception:
                os._exit(1)
        else:
            # Parent process
            os.close(fd)  # Close slave in parent

            # Set master fd to non-blocking
            flags = fcntl.fcntl(pid, fcntl.F_GETFL)
            fcntl.fcntl(pid, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            # Set initial window size on master
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                fcntl.ioctl(pid, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

            session = PTYSession(
                session_id=session_id,
                pid=child_pid,
                fd=pid,
                shell=shell,
                cols=cols,
                rows=rows,
                _output_callback=output_callback,
                _exit_callback=exit_callback,
            )

            self._sessions[session_id] = session

            # Start async reader
            session._reader_task = asyncio.create_task(
                self._read_loop(session)
            )

            logger.info(
                "Created session %s (shell=%s, pid=%d)",
                session_id, shell, child_pid,
            )
            return session

    async def write_to_session(self, session_id: str, data: str) -> None:
        """Write data (base64 encoded) to a PTY session."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        raw = base64.b64decode(data)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, os.write, session.fd, raw)
        session.last_activity = time.time()

    async def resize_session(self, session_id: str, cols: int, rows: int) -> None:
        """Resize a PTY session."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(session.fd, termios.TIOCSWINSZ, winsize)
            # Signal the child process about the resize
            os.kill(session.pid, signal.SIGWINCH)
        except OSError as e:
            logger.warning("Resize failed for session %s: %s", session_id, e)

        session.cols = cols
        session.rows = rows

    async def destroy_session(self, session_id: str) -> int | None:
        """Destroy a PTY session and return exit code."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return None

        # Cancel reader task
        if session._reader_task and not session._reader_task.done():
            session._reader_task.cancel()
            try:
                await session._reader_task
            except asyncio.CancelledError:
                pass

        exit_code = await self._cleanup_session(session)
        logger.info("Destroyed session %s (exit_code=%s)", session_id, exit_code)
        return exit_code

    async def destroy_all(self) -> None:
        """Destroy all sessions (used on shutdown)."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.destroy_session(sid)

    async def exec_command(
        self,
        command: str,
        timeout: float = 30.0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """Execute a one-shot command and return the result.

        This uses subprocess (not PTY) for clean stdout/stderr capture.
        """
        child_env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_VARS}
        child_env["TERMINAL_BRIDGE"] = "1"
        if env:
            child_env.update(env)

        start = time.time()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            exit_code = proc.returncode or 0
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()  # type: ignore[possibly-undefined]
                await proc.wait()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            stdout_bytes = b""
            stderr_bytes = b"Command timed out"
            exit_code = -1

        duration = time.time() - start
        return {
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "duration": round(duration, 3),
            "timed_out": timed_out,
        }

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    async def _read_loop(self, session: PTYSession) -> None:
        """Continuously read output from PTY and invoke callback."""
        loop = asyncio.get_event_loop()
        try:
            while True:
                # Wait for data to be available using select in executor
                readable = await loop.run_in_executor(
                    None, self._wait_readable, session.fd, 0.1
                )
                if not readable:
                    # Check if child is still alive
                    if not self._is_alive(session.pid):
                        break
                    continue

                try:
                    data = os.read(session.fd, 65536)
                except OSError:
                    break

                if not data:
                    break

                session.last_activity = time.time()

                if session._output_callback:
                    encoded = base64.b64encode(data).decode("ascii")
                    try:
                        session._output_callback(session.session_id, encoded)
                    except Exception as e:
                        logger.error("Output callback error: %s", e)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Read loop error for session %s: %s", session.session_id, e)
        finally:
            # Session ended
            exit_code = await self._cleanup_session(session)
            self._sessions.pop(session.session_id, None)
            if session._exit_callback:
                try:
                    session._exit_callback(session.session_id, exit_code or 0)
                except Exception:
                    pass

    @staticmethod
    def _wait_readable(fd: int, timeout: float) -> bool:
        """Block until fd is readable or timeout (for use in executor)."""
        try:
            readable, _, _ = select.select([fd], [], [], timeout)
            return bool(readable)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _is_alive(pid: int) -> bool:
        """Check if a process is still alive."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    async def _cleanup_session(session: PTYSession) -> int | None:
        """Clean up a session: close fd, kill process, get exit code."""
        # Close fd
        try:
            os.close(session.fd)
        except OSError:
            pass

        # Send SIGHUP then SIGKILL if needed
        exit_code = None
        try:
            os.kill(session.pid, signal.SIGHUP)
            for _ in range(10):
                pid, status = os.waitpid(session.pid, os.WNOHANG)
                if pid != 0:
                    if os.WIFEXITED(status):
                        exit_code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        exit_code = -os.WTERMSIG(status)
                    break
                await asyncio.sleep(0.1)
            else:
                # Force kill
                os.kill(session.pid, signal.SIGKILL)
                os.waitpid(session.pid, 0)
                exit_code = -9
        except (OSError, ChildProcessError):
            pass

        return exit_code

