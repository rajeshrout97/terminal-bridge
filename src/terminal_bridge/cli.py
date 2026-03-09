"""CLI entry point for Terminal Bridge.

Usage:
    tbridge setup remote          # Configure this Mac as a remote agent
    tbridge setup local <CODE>    # Pair with a remote Mac
    tbridge agent start|stop      # Manage the remote agent service
    tbridge connect <remote>      # Connect to a remote Mac
    tbridge exec <command>        # Run a command on a remote Mac
    tbridge sessions list         # List active sessions
    tbridge file push/pull        # Transfer files
    tbridge status                # Show connection health
    tbridge relay start           # Start relay server
    tbridge pipe                  # Stdio JSON pipe mode
"""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console

from terminal_bridge.version import __version__

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="terminal-bridge")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Terminal Bridge -- Remote Mac terminal access for any AI agent."""
    ctx.ensure_object(dict)


# ---------------------------------------------------------------------------
# Setup commands
# ---------------------------------------------------------------------------
@main.group()
def setup() -> None:
    """First-time setup wizards."""


@setup.command("remote")
def setup_remote() -> None:
    """Set up this Mac as a remote agent (the Mac you want to control)."""
    from terminal_bridge.setup.wizard import run_remote_setup

    asyncio.run(run_remote_setup())


@setup.command("local")
@click.argument("pairing_code")
def setup_local(pairing_code: str) -> None:
    """Pair with a remote Mac using a pairing code."""
    from terminal_bridge.setup.wizard import run_local_setup

    asyncio.run(run_local_setup(pairing_code))


@setup.command("relay")
@click.option("--port", default=9878, help="Relay server port.")
def setup_relay(port: int) -> None:
    """Set up a relay server for internet connections."""
    from terminal_bridge.setup.wizard import run_relay_setup

    asyncio.run(run_relay_setup(port))


# ---------------------------------------------------------------------------
# Agent commands
# ---------------------------------------------------------------------------
@main.group()
def agent() -> None:
    """Manage the remote agent service."""


@agent.command("start")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize).")
@click.option("--port", "-p", default=None, type=int, help="Override agent port.")
def agent_start(foreground: bool, port: int | None) -> None:
    """Start the remote agent."""
    from terminal_bridge.remote_agent.server import start_agent

    asyncio.run(start_agent(foreground=foreground, port=port))


@agent.command("stop")
def agent_stop() -> None:
    """Stop the remote agent."""
    from terminal_bridge.setup.launchd import stop_agent_service

    stop_agent_service()
    console.print("[green]Agent stopped.[/green]")


@agent.command("status")
def agent_status() -> None:
    """Show agent status."""
    from terminal_bridge.setup.launchd import get_agent_status

    status = get_agent_status()
    if status["running"]:
        console.print(f"[green]Agent running[/green] (PID {status['pid']}) on port {status['port']}")
    else:
        console.print("[yellow]Agent not running.[/yellow]")


# ---------------------------------------------------------------------------
# Connect command
# ---------------------------------------------------------------------------
@main.command()
@click.argument("remote")
@click.option("--name", "-n", default=None, help="Name for this remote connection.")
@click.option("--terminal", "-t", is_flag=True, help="Open interactive terminal.")
def connect(remote: str, name: str | None, terminal: bool) -> None:
    """Connect to a remote Mac (IP, hostname, or relay://room-id)."""
    from terminal_bridge.local_bridge.client import connect_to_remote

    asyncio.run(connect_to_remote(remote, name=name, interactive=terminal))


# ---------------------------------------------------------------------------
# Exec command
# ---------------------------------------------------------------------------
@main.command("exec")
@click.argument("command")
@click.option("--remote", "-r", default=None, help="Remote name (if multiple).")
@click.option("--session", "-s", default=None, help="Use a persistent session.")
@click.option("--timeout", "-t", default=30, help="Command timeout in seconds.")
def exec_cmd(command: str, remote: str | None, session: str | None, timeout: int) -> None:
    """Execute a command on the remote Mac."""
    from terminal_bridge.local_bridge.client import exec_remote_command

    result = asyncio.run(exec_remote_command(command, remote=remote, session=session, timeout=timeout))
    if result.get("stdout"):
        click.echo(result["stdout"], nl=False)
    if result.get("stderr"):
        click.echo(result["stderr"], nl=False, err=True)
    sys.exit(result.get("exit_code", 0))


# ---------------------------------------------------------------------------
# Sessions command
# ---------------------------------------------------------------------------
@main.group()
def sessions() -> None:
    """Manage remote terminal sessions."""


@sessions.command("list")
@click.option("--remote", "-r", default=None, help="Remote name.")
def sessions_list(remote: str | None) -> None:
    """List active sessions on the remote Mac."""
    from terminal_bridge.local_bridge.client import list_remote_sessions

    sessions_data = asyncio.run(list_remote_sessions(remote=remote))
    if not sessions_data:
        console.print("[yellow]No active sessions.[/yellow]")
        return
    from rich.table import Table

    table = Table(title="Active Sessions")
    table.add_column("ID", style="cyan")
    table.add_column("Shell", style="green")
    table.add_column("Started", style="magenta")
    table.add_column("Idle", style="yellow")
    for s in sessions_data:
        table.add_row(s["id"], s["shell"], s["started"], s["idle"])
    console.print(table)


# ---------------------------------------------------------------------------
# File commands
# ---------------------------------------------------------------------------
@main.group()
def file() -> None:
    """Transfer files to/from the remote Mac."""


@file.command("push")
@click.argument("local_path")
@click.argument("remote_path")
@click.option("--remote", "-r", default=None, help="Remote name.")
def file_push(local_path: str, remote_path: str, remote: str | None) -> None:
    """Upload a file to the remote Mac."""
    from terminal_bridge.local_bridge.client import push_file

    asyncio.run(push_file(local_path, remote_path, remote=remote))
    console.print(f"[green]Pushed {local_path} -> {remote_path}[/green]")


@file.command("pull")
@click.argument("remote_path")
@click.argument("local_path")
@click.option("--remote", "-r", default=None, help="Remote name.")
def file_pull(remote_path: str, local_path: str, remote: str | None) -> None:
    """Download a file from the remote Mac."""
    from terminal_bridge.local_bridge.client import pull_file

    asyncio.run(pull_file(remote_path, local_path, remote=remote))
    console.print(f"[green]Pulled {remote_path} -> {local_path}[/green]")


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------
@main.command()
@click.option("--remote", "-r", default=None, help="Remote name.")
def status(remote: str | None) -> None:
    """Show connection health and status."""
    from terminal_bridge.local_bridge.client import get_status

    info = asyncio.run(get_status(remote=remote))
    console.print(f"[bold]Connection:[/bold] {info.get('mode', 'unknown')}")
    console.print(f"[bold]Remote:[/bold] {info.get('hostname', 'unknown')}")
    console.print(f"[bold]Latency:[/bold] {info.get('latency_ms', '?')}ms")
    console.print(f"[bold]Sessions:[/bold] {info.get('active_sessions', 0)}")
    console.print(f"[bold]Uptime:[/bold] {info.get('uptime', 'unknown')}")


# ---------------------------------------------------------------------------
# Relay command
# ---------------------------------------------------------------------------
@main.group()
def relay() -> None:
    """Manage the relay server."""


@relay.command("start")
@click.option("--port", "-p", default=9878, help="Relay server port.")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground.")
def relay_start(port: int, foreground: bool) -> None:
    """Start the relay server."""
    from terminal_bridge.relay.server import start_relay

    asyncio.run(start_relay(port=port, foreground=foreground))


# ---------------------------------------------------------------------------
# Pipe command (stdio JSON for Claude CLI, aider, etc.)
# ---------------------------------------------------------------------------
@main.command()
@click.option("--remote", "-r", default=None, help="Remote name.")
def pipe(remote: str | None) -> None:
    """Stdio JSON pipe mode for tool-use integration."""
    from terminal_bridge.local_bridge.stdio_pipe import run_stdio_pipe

    asyncio.run(run_stdio_pipe(remote=remote))


# ---------------------------------------------------------------------------
# MCP command
# ---------------------------------------------------------------------------
@main.command()
@click.option("--remote", "-r", default=None, help="Remote name.")
def mcp(remote: str | None) -> None:
    """Start the MCP server for Cursor integration."""
    from terminal_bridge.local_bridge.mcp_server import run_mcp_server

    run_mcp_server(remote=remote)


# ---------------------------------------------------------------------------
# API command
# ---------------------------------------------------------------------------
@main.command()
@click.option("--remote", "-r", default=None, help="Remote name.")
@click.option("--port", "-p", default=9876, help="API port.")
def api(remote: str | None, port: int) -> None:
    """Start the REST API server for Ollama/any AI tool."""
    from terminal_bridge.local_bridge.rest_api import run_rest_api

    asyncio.run(run_rest_api(remote=remote, port=port))


# ---------------------------------------------------------------------------
# Enterprise admin commands
# ---------------------------------------------------------------------------
try:
    from terminal_bridge.enterprise.cli import admin
    main.add_command(admin)
except ImportError:
    pass


if __name__ == "__main__":
    main()

