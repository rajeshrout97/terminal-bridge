"""launchd service management for Terminal Bridge on macOS.

Creates and manages LaunchAgent plists so the Terminal Bridge agent
runs automatically in the background on login.

Plist location: ~/Library/LaunchAgents/com.terminal-bridge.agent.plist
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from terminal_bridge.config import DEFAULT_AGENT_PORT, load_config

logger = logging.getLogger(__name__)

AGENT_LABEL = "com.terminal-bridge.agent"
BRIDGE_LABEL = "com.terminal-bridge.bridge"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
AGENT_PLIST = LAUNCH_AGENTS_DIR / f"{AGENT_LABEL}.plist"
BRIDGE_PLIST = LAUNCH_AGENTS_DIR / f"{BRIDGE_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "terminal-bridge"


def _get_tbridge_path() -> str:
    """Get the path to the tbridge command."""
    # Try shutil.which first
    path = shutil.which("tbridge")
    if path:
        return path

    # Try the current Python's scripts directory
    scripts_dir = Path(sys.executable).parent
    tbridge_path = scripts_dir / "tbridge"
    if tbridge_path.exists():
        return str(tbridge_path)

    # Fallback: use python -m
    return f"{sys.executable} -m terminal_bridge.cli"


def _ensure_dirs() -> None:
    """Ensure required directories exist."""
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def create_agent_plist(port: int | None = None) -> Path:
    """Create the launchd plist for the remote agent."""
    _ensure_dirs()

    if port is None:
        config = load_config()
        port = config.get("agent", {}).get("port", DEFAULT_AGENT_PORT)

    tbridge = _get_tbridge_path()

    # Build the command
    if " -m " in tbridge:
        parts = tbridge.split()
        program_args = parts + ["agent", "start", "--foreground", "--port", str(port)]
    else:
        program_args = [tbridge, "agent", "start", "--foreground", "--port", str(port)]

    plist_data = {
        "Label": AGENT_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,  # Restart if it crashes
        },
        "StandardOutPath": str(LOG_DIR / "agent.log"),
        "StandardErrorPath": str(LOG_DIR / "agent-error.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(Path.home()),
        },
        "ProcessType": "Background",
        "ThrottleInterval": 5,
    }

    with open(AGENT_PLIST, "wb") as f:
        plistlib.dump(plist_data, f)

    logger.info("Created agent plist: %s", AGENT_PLIST)
    return AGENT_PLIST


def create_bridge_plist(remote_name: str | None = None, api_port: int = 9876) -> Path:
    """Create the launchd plist for the local bridge (REST API + MCP)."""
    _ensure_dirs()

    tbridge = _get_tbridge_path()

    if " -m " in tbridge:
        parts = tbridge.split()
        program_args = parts + ["api", "--port", str(api_port)]
    else:
        program_args = [tbridge, "api", "--port", str(api_port)]

    if remote_name:
        program_args.extend(["--remote", remote_name])

    plist_data = {
        "Label": BRIDGE_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,
        },
        "StandardOutPath": str(LOG_DIR / "bridge.log"),
        "StandardErrorPath": str(LOG_DIR / "bridge-error.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(Path.home()),
        },
        "ProcessType": "Background",
        "ThrottleInterval": 5,
    }

    with open(BRIDGE_PLIST, "wb") as f:
        plistlib.dump(plist_data, f)

    logger.info("Created bridge plist: %s", BRIDGE_PLIST)
    return BRIDGE_PLIST


def load_service(plist_path: Path) -> bool:
    """Load a launchd service from a plist."""
    try:
        subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            check=True,
            capture_output=True,
        )
        logger.info("Loaded service: %s", plist_path)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Failed to load service: %s", e.stderr.decode())
        return False


def unload_service(plist_path: Path) -> bool:
    """Unload a launchd service."""
    try:
        subprocess.run(
            ["launchctl", "unload", "-w", str(plist_path)],
            check=True,
            capture_output=True,
        )
        logger.info("Unloaded service: %s", plist_path)
        return True
    except subprocess.CalledProcessError:
        return False


def start_agent_service(port: int | None = None) -> bool:
    """Create and start the agent launchd service."""
    plist = create_agent_plist(port=port)
    # Unload first if already loaded
    unload_service(plist)
    return load_service(plist)


def stop_agent_service() -> bool:
    """Stop the agent launchd service."""
    if AGENT_PLIST.exists():
        return unload_service(AGENT_PLIST)
    return False


def start_bridge_service(remote_name: str | None = None, api_port: int = 9876) -> bool:
    """Create and start the bridge launchd service."""
    plist = create_bridge_plist(remote_name=remote_name, api_port=api_port)
    unload_service(plist)
    return load_service(plist)


def stop_bridge_service() -> bool:
    """Stop the bridge launchd service."""
    if BRIDGE_PLIST.exists():
        return unload_service(BRIDGE_PLIST)
    return False


def get_agent_status() -> dict:
    """Get the status of the agent launchd service."""
    try:
        result = subprocess.run(
            ["launchctl", "list", AGENT_LABEL],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # Parse output to get PID
            lines = result.stdout.strip().split("\n")
            for line in lines:
                parts = line.split("\t")
                if len(parts) >= 3 and AGENT_LABEL in parts[2]:
                    pid = parts[0] if parts[0] != "-" else None
                    config = load_config()
                    port = config.get("agent", {}).get("port", DEFAULT_AGENT_PORT)
                    return {
                        "running": pid is not None,
                        "pid": pid,
                        "port": port,
                        "plist": str(AGENT_PLIST),
                    }
        return {"running": False, "pid": None, "port": None, "plist": str(AGENT_PLIST)}
    except Exception:
        return {"running": False, "pid": None, "port": None, "plist": str(AGENT_PLIST)}


def remove_services() -> None:
    """Stop and remove all Terminal Bridge launchd services."""
    stop_agent_service()
    stop_bridge_service()
    for plist in (AGENT_PLIST, BRIDGE_PLIST):
        if plist.exists():
            plist.unlink()
            logger.info("Removed plist: %s", plist)

