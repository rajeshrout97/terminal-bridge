"""Pairing code generation and QR code display for Terminal Bridge.

Pairing codes encode connection information (secret key, host, port, hostname)
in a short, human-readable format that can be shared between Macs.
"""

from __future__ import annotations

import io
import socket
import sys

from rich.console import Console
from rich.panel import Panel

from terminal_bridge.config import DEFAULT_AGENT_PORT
from terminal_bridge.security.tokens import decode_pairing_code, generate_pairing_code


def get_local_ips() -> list[str]:
    """Get all local IP addresses for this machine."""
    ips = []
    try:
        # Get primary IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    # Also try hostname resolution
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass

    return ips or ["127.0.0.1"]


def create_pairing_code(
    secret_key: str,
    port: int = DEFAULT_AGENT_PORT,
) -> str:
    """Create a pairing code for this machine."""
    ips = get_local_ips()
    hostname = socket.gethostname()
    # Use the primary IP
    host = ips[0]
    return generate_pairing_code(secret_key, host, port, hostname)


def display_pairing_info(
    pairing_code: str,
    port: int = DEFAULT_AGENT_PORT,
) -> None:
    """Display pairing code and QR code in the terminal."""
    console = Console()
    ips = get_local_ips()
    hostname = socket.gethostname()

    # Display pairing code prominently
    console.print()
    console.print(
        Panel(
            f"[bold cyan]{pairing_code}[/bold cyan]",
            title="[bold green]Pairing Code[/bold green]",
            subtitle="Share this code with the other Mac",
            padding=(1, 4),
        )
    )

    # Display QR code if qrcode library is available
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=1,
        )
        qr.add_data(pairing_code)
        qr.make(fit=True)

        # Render QR code as text
        buffer = io.StringIO()
        qr.print_ascii(out=buffer, invert=True)
        qr_text = buffer.getvalue()

        console.print(
            Panel(
                qr_text,
                title="[bold]QR Code[/bold]",
                subtitle="Scan to copy pairing code",
            )
        )
    except ImportError:
        console.print("[dim]Install qrcode for QR display: pip install qrcode[/dim]")
    except Exception:
        pass

    # Display connection info
    console.print(f"\n[bold]Hostname:[/bold] {hostname}")
    console.print(f"[bold]Port:[/bold] {port}")
    console.print(f"[bold]IP addresses:[/bold]")
    for ip in ips:
        console.print(f"  • {ip}")

    console.print(
        f"\n[dim]On the other Mac, run:[/dim]"
        f"\n  [bold green]tbridge setup local {pairing_code}[/bold green]\n"
    )


def verify_pairing_code(code: str) -> dict | None:
    """Verify and decode a pairing code. Returns decoded info or None."""
    try:
        info = decode_pairing_code(code)
        if not all(k in info for k in ("k", "h", "p", "n")):
            return None
        return info
    except Exception:
        return None

