"""Auto-configure Cursor MCP for Terminal Bridge.

Adds the Terminal Bridge MCP server to Cursor's configuration
so the AI agent can use remote terminal tools natively.

Config file: ~/.cursor/mcp.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CURSOR_CONFIG_DIR = Path.home() / ".cursor"
MCP_CONFIG_FILE = CURSOR_CONFIG_DIR / "mcp.json"


def configure_cursor_mcp(remote_name: str | None = None) -> bool:
    """Add Terminal Bridge to Cursor's MCP configuration.

    Creates or updates ~/.cursor/mcp.json with the terminal-bridge server.
    """
    try:
        CURSOR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing config
        if MCP_CONFIG_FILE.exists():
            with open(MCP_CONFIG_FILE) as f:
                config = json.load(f)
        else:
            config = {}

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        # Build the tbridge command args
        args = ["mcp"]
        if remote_name:
            args.extend(["--remote", remote_name])

        config["mcpServers"]["terminal-bridge"] = {
            "command": "tbridge",
            "args": args,
        }

        # Write config
        with open(MCP_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

        logger.info("Configured Cursor MCP at %s", MCP_CONFIG_FILE)
        return True

    except Exception as e:
        logger.warning("Failed to configure Cursor MCP: %s", e)
        return False


def remove_cursor_mcp() -> bool:
    """Remove Terminal Bridge from Cursor's MCP configuration."""
    try:
        if not MCP_CONFIG_FILE.exists():
            return True

        with open(MCP_CONFIG_FILE) as f:
            config = json.load(f)

        if "mcpServers" in config and "terminal-bridge" in config["mcpServers"]:
            del config["mcpServers"]["terminal-bridge"]

            with open(MCP_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)

            logger.info("Removed Terminal Bridge from Cursor MCP config")

        return True
    except Exception as e:
        logger.warning("Failed to remove Cursor MCP config: %s", e)
        return False


def check_cursor_configured() -> bool:
    """Check if Terminal Bridge is configured in Cursor."""
    try:
        if not MCP_CONFIG_FILE.exists():
            return False
        with open(MCP_CONFIG_FILE) as f:
            config = json.load(f)
        return "terminal-bridge" in config.get("mcpServers", {})
    except Exception:
        return False

