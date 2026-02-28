"""macOS firewall management for Terminal Bridge.

Registers the Terminal Bridge agent with the macOS Application Firewall
so incoming connections are allowed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


def get_python_path() -> str:
    """Get the path to the current Python interpreter."""
    return sys.executable


def add_firewall_exception() -> bool:
    """Add Python to the macOS firewall allow list.

    Note: This requires admin privileges and may prompt for password.
    The socketfilterfw command is used to manage the macOS Application Firewall.
    """
    python_path = get_python_path()

    # Check if socketfilterfw exists
    fw_tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    if not shutil.which(fw_tool) and not __import__("pathlib").Path(fw_tool).exists():
        logger.info("macOS firewall tool not found. Skipping firewall setup.")
        return True  # Not a failure, firewall might be disabled

    try:
        # Add the Python interpreter to the allow list
        result = subprocess.run(
            ["sudo", fw_tool, "--add", python_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("Could not add firewall exception: %s", result.stderr)
            return False

        # Explicitly allow incoming connections
        result = subprocess.run(
            ["sudo", fw_tool, "--unblockapp", python_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("Added firewall exception for %s", python_path)
        return True

    except subprocess.TimeoutExpired:
        logger.warning("Firewall setup timed out (may need manual approval)")
        return False
    except Exception as e:
        logger.warning("Firewall setup failed: %s", e)
        return False


def check_firewall_status() -> dict:
    """Check the macOS firewall status."""
    fw_tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    try:
        result = subprocess.run(
            [fw_tool, "--getglobalstate"],
            capture_output=True,
            text=True,
        )
        enabled = "enabled" in result.stdout.lower()
        return {"enabled": enabled, "output": result.stdout.strip()}
    except Exception:
        return {"enabled": False, "output": "Could not check firewall"}


def show_firewall_instructions() -> None:
    """Print manual firewall instructions."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print(
        Panel(
            "[yellow]If connections are blocked, you may need to:[/yellow]\n\n"
            "1. Open System Preferences > Security & Privacy > Firewall\n"
            "2. Click 'Firewall Options'\n"
            "3. Add Python or Terminal Bridge to the allowed list\n"
            "4. Or run: sudo /usr/libexec/ApplicationFirewall/socketfilterfw "
            f"--add {get_python_path()}\n",
            title="[bold]Firewall Setup[/bold]",
        )
    )

