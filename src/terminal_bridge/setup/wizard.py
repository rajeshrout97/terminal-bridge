"""Setup wizards for Terminal Bridge.

Provides guided setup for:
    - Remote agent (tbridge setup remote)
    - Local bridge (tbridge setup local <PAIRING_CODE>)
    - Relay server (tbridge setup relay)
"""

from __future__ import annotations

import asyncio
import platform
import socket
import sys

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from terminal_bridge.config import (
    DEFAULT_AGENT_PORT,
    DEFAULT_API_PORT,
    DEFAULT_RELAY_PORT,
    add_remote_config,
    default_config,
    load_config,
    save_config,
)
from terminal_bridge.security.keychain import retrieve_secret, store_secret
from terminal_bridge.security.tokens import generate_secret_key
from terminal_bridge.setup.pairing import (
    create_pairing_code,
    display_pairing_info,
    get_local_ips,
    verify_pairing_code,
)
from terminal_bridge.version import __version__

console = Console()


async def run_remote_setup() -> None:
    """Set up this Mac as a remote agent (the Mac you want to control).

    Steps:
    1. Check macOS compatibility
    2. Generate secret key
    3. Store key in Keychain
    4. Create config file
    5. Generate TLS certificate
    6. Register Bonjour service
    7. Create launchd plist
    8. Setup firewall
    9. Generate pairing code
    10. Display pairing info
    """
    console.print(
        Panel(
            f"[bold]Terminal Bridge v{__version__} - Remote Agent Setup[/bold]\n\n"
            "This wizard will configure this Mac as a remote agent.\n"
            "Other Macs can then connect and execute commands here.",
            title="[green]Setup[/green]",
        )
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:

        # Step 1: Check macOS
        task = progress.add_task("Checking macOS compatibility...", total=None)
        mac_ver = platform.mac_ver()[0]
        if not mac_ver:
            console.print("[yellow]Warning: Could not detect macOS version.[/yellow]")
        else:
            console.print(f"  macOS {mac_ver} ({platform.machine()})")
        progress.update(task, completed=True)

        # Step 2: Generate secret key
        task = progress.add_task("Generating secret key...", total=None)
        existing_key = retrieve_secret("agent_key")
        if existing_key:
            console.print("  [dim]Using existing key from Keychain[/dim]")
            secret_key = existing_key
        else:
            secret_key = generate_secret_key()
            console.print("  [green]Generated 256-bit secret key[/green]")
        progress.update(task, completed=True)

        # Step 3: Store in Keychain
        task = progress.add_task("Storing key in macOS Keychain...", total=None)
        store_secret("agent_key", secret_key)
        console.print("  [green]Key stored securely[/green]")
        progress.update(task, completed=True)

        # Step 4: Create config
        task = progress.add_task("Creating configuration...", total=None)
        config = load_config()
        if config.get("version") is None:
            config = default_config()
        save_config(config)
        console.print(f"  Config: ~/.config/terminal-bridge/config.yaml")
        progress.update(task, completed=True)

        # Step 5: Generate TLS certificate
        task = progress.add_task("Generating TLS certificate...", total=None)
        try:
            from terminal_bridge.security.tls import generate_self_signed_cert

            cert_path, key_path = generate_self_signed_cert(
                hostname=socket.gethostname()
            )
            console.print(f"  [green]Certificate generated[/green]")
        except Exception as e:
            console.print(f"  [yellow]TLS setup skipped: {e}[/yellow]")
        progress.update(task, completed=True)

        # Step 6: Register Bonjour
        task = progress.add_task("Registering Bonjour service...", total=None)
        try:
            from terminal_bridge.setup.bonjour import BonjourService

            port = config.get("agent", {}).get("port", DEFAULT_AGENT_PORT)
            bonjour = BonjourService(port=port)
            if bonjour.register():
                console.print("  [green]LAN discovery enabled[/green]")
            else:
                console.print("  [yellow]Bonjour unavailable[/yellow]")
        except Exception as e:
            console.print(f"  [yellow]Bonjour skipped: {e}[/yellow]")
        progress.update(task, completed=True)

        # Step 7: Create launchd plist
        task = progress.add_task("Creating launchd service...", total=None)
        try:
            from terminal_bridge.setup.launchd import start_agent_service

            if start_agent_service(port=port):
                console.print("  [green]Agent service installed (auto-starts on login)[/green]")
            else:
                console.print("  [yellow]Launchd setup failed. Start manually: tbridge agent start[/yellow]")
        except Exception as e:
            console.print(f"  [yellow]Launchd skipped: {e}[/yellow]")
        progress.update(task, completed=True)

        # Step 8: Firewall
        task = progress.add_task("Configuring firewall...", total=None)
        try:
            from terminal_bridge.setup.firewall import (
                add_firewall_exception,
                show_firewall_instructions,
            )

            if add_firewall_exception():
                console.print("  [green]Firewall configured[/green]")
            else:
                console.print("  [yellow]Firewall may need manual setup[/yellow]")
                show_firewall_instructions()
        except Exception as e:
            console.print(f"  [dim]Firewall setup skipped: {e}[/dim]")
        progress.update(task, completed=True)

    # Step 9-10: Generate and display pairing code
    console.print("\n[bold green]Setup complete![/bold green]\n")

    port = config.get("agent", {}).get("port", DEFAULT_AGENT_PORT)
    pairing_code = create_pairing_code(secret_key, port=port)
    display_pairing_info(pairing_code, port=port)


async def run_local_setup(pairing_code: str) -> None:
    """Pair with a remote Mac using a pairing code.

    Steps:
    1. Decode pairing code
    2. Store remote key in Keychain
    3. Try LAN discovery
    4. Test connection
    5. Configure Cursor MCP
    6. Create config
    7. Start local bridge service
    """
    console.print(
        Panel(
            f"[bold]Terminal Bridge v{__version__} - Local Bridge Setup[/bold]\n\n"
            "This wizard will pair with a remote Mac.",
            title="[green]Setup[/green]",
        )
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:

        # Step 1: Decode pairing code
        task = progress.add_task("Decoding pairing code...", total=None)
        info = verify_pairing_code(pairing_code)
        if info is None:
            console.print("[red]Invalid pairing code. Please check and try again.[/red]")
            sys.exit(1)

        remote_key = info["k"]
        remote_host = info["h"]
        remote_port = info["p"]
        remote_hostname = info["n"]
        console.print(f"  Remote: [cyan]{remote_hostname}[/cyan] ({remote_host}:{remote_port})")
        progress.update(task, completed=True)

        # Step 2: Store remote key
        task = progress.add_task("Storing remote key in Keychain...", total=None)
        remote_name = remote_hostname.replace(".local", "").replace(".", "-").lower()
        store_secret(f"remote_{remote_name}", remote_key)
        store_secret("remote_default", remote_key)
        console.print("  [green]Key stored securely[/green]")
        progress.update(task, completed=True)

        # Step 3: Try LAN discovery
        task = progress.add_task("Searching local network...", total=None)
        discovered_host = None
        try:
            from terminal_bridge.setup.bonjour import discover_agents

            agents = await discover_agents(timeout=3.0)
            for agent in agents:
                if agent["hostname"].lower().startswith(remote_name):
                    discovered_host = agent["ip"]
                    remote_port = agent["port"]
                    console.print(f"  [green]Found {agent['hostname']} at {agent['ip']}[/green]")
                    break
            if not discovered_host:
                console.print(f"  [dim]Using provided address: {remote_host}[/dim]")
        except Exception:
            console.print(f"  [dim]Using provided address: {remote_host}[/dim]")
        progress.update(task, completed=True)

        final_host = discovered_host or remote_host

        # Step 4: Test connection
        task = progress.add_task("Testing connection...", total=None)
        try:
            from terminal_bridge.local_bridge.client import RemoteTerminal

            rt = RemoteTerminal(
                host=final_host,
                port=remote_port,
                secret_key=remote_key,
            )
            await rt.connect()
            latency = await rt.ping()
            info_result = await rt.system_info()
            await rt.disconnect()
            console.print(
                f"  [green]Connected! Latency: {latency}ms, "
                f"Host: {info_result['hostname']}[/green]"
            )
        except Exception as e:
            console.print(f"  [yellow]Connection test failed: {e}[/yellow]")
            console.print("  [dim]The remote agent may not be running yet.[/dim]")
        progress.update(task, completed=True)

        # Step 5: Save remote config
        task = progress.add_task("Saving configuration...", total=None)
        add_remote_config(remote_name, {
            "host": final_host,
            "port": remote_port,
            "hostname": remote_hostname,
        })
        console.print(f"  Remote '{remote_name}' saved to config")
        progress.update(task, completed=True)

        # Step 6: Configure Cursor MCP
        task = progress.add_task("Configuring Cursor MCP...", total=None)
        try:
            from terminal_bridge.setup.cursor_config import configure_cursor_mcp

            if configure_cursor_mcp(remote_name=remote_name):
                console.print("  [green]Cursor MCP configured[/green]")
            else:
                console.print("  [yellow]Cursor MCP setup skipped[/yellow]")
        except Exception as e:
            console.print(f"  [dim]Cursor config skipped: {e}[/dim]")
        progress.update(task, completed=True)

        # Step 7: Start bridge service
        task = progress.add_task("Starting local bridge service...", total=None)
        try:
            from terminal_bridge.setup.launchd import start_bridge_service

            if start_bridge_service(remote_name=remote_name):
                console.print("  [green]Bridge service started (REST API on :9876)[/green]")
            else:
                console.print(
                    "  [yellow]Auto-start failed. Run manually: tbridge api[/yellow]"
                )
        except Exception as e:
            console.print(f"  [dim]Bridge service skipped: {e}[/dim]")
        progress.update(task, completed=True)

    # Summary
    console.print(f"\n[bold green]Pairing complete![/bold green]\n")
    console.print(f"[bold]Remote:[/bold] {remote_hostname} ({final_host}:{remote_port})")
    console.print(f"[bold]Name:[/bold] {remote_name}")
    console.print(f"\n[bold]Available interfaces:[/bold]")
    console.print(f"  • CLI:       tbridge exec 'ls -la'")
    console.print(f"  • REST API:  http://127.0.0.1:9876/api/exec")
    console.print(f"  • MCP:       Auto-configured in Cursor")
    console.print(f"  • Pipe:      echo '{{\"tool\":\"exec\",\"command\":\"ls\"}}' | tbridge pipe")
    console.print(f"  • Terminal:  tbridge connect {remote_name} --terminal")
    console.print(f"  • Python:    from terminal_bridge import RemoteTerminal")
    console.print()


async def run_relay_setup(port: int = DEFAULT_RELAY_PORT) -> None:
    """Set up a relay server.

    Generates a relay URL and auth token that can be shared.
    """
    console.print(
        Panel(
            f"[bold]Terminal Bridge v{__version__} - Relay Server Setup[/bold]\n\n"
            "This sets up a relay for internet connections between Macs.",
            title="[green]Setup[/green]",
        )
    )

    from terminal_bridge.security.tokens import generate_secret_key

    relay_token = generate_secret_key()[:16]

    # Get public IP for display
    ips = get_local_ips()

    console.print(f"\n[bold]Relay server configuration:[/bold]")
    console.print(f"  Port:  {port}")
    console.print(f"  Token: [cyan]{relay_token}[/cyan]")
    console.print(f"  IPs:   {', '.join(ips)}")
    console.print(f"\n[bold]To start the relay:[/bold]")
    console.print(f"  tbridge relay start --port {port}")
    console.print(f"\n[bold]On the remote Mac:[/bold]")
    console.print(f"  Set relay URL in config: relay://<your-server-ip>:{port}")
    console.print(f"  Token: {relay_token}")
    console.print()

    # Store relay config
    config = load_config()
    config["relay"] = {
        "host": "0.0.0.0",
        "port": port,
        "token": relay_token,
    }
    save_config(config)
    console.print("[green]Relay configuration saved.[/green]\n")

