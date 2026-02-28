"""Configuration management for Terminal Bridge.

Config file: ~/.config/terminal-bridge/config.yaml
Secrets: stored in macOS Keychain via `keyring`
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".config" / "terminal-bridge"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
CERTS_DIR = CONFIG_DIR / "certs"
SERVICE_NAME = "terminal-bridge"
DEFAULT_AGENT_PORT = 9877
DEFAULT_API_PORT = 9876
DEFAULT_RELAY_PORT = 9878


def get_config_dir() -> Path:
    """Get or create the configuration directory."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def get_certs_dir() -> Path:
    """Get or create the certificates directory."""
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    return CERTS_DIR


def load_config() -> dict[str, Any]:
    """Load configuration from YAML file."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return default_config()


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to YAML file."""
    get_config_dir()
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    # Restrict permissions to owner only
    os.chmod(CONFIG_FILE, 0o600)


def default_config() -> dict[str, Any]:
    """Return the default configuration."""
    return {
        "version": 1,
        "agent": {
            "host": "0.0.0.0",
            "port": DEFAULT_AGENT_PORT,
            "shell": os.environ.get("SHELL", "/bin/zsh"),
            "max_sessions": 10,
            "idle_timeout": 3600,
            "enable_bonjour": True,
        },
        "bridge": {
            "api_host": "127.0.0.1",
            "api_port": DEFAULT_API_PORT,
            "auto_reconnect": True,
            "reconnect_delay": 2,
            "max_reconnect_delay": 60,
        },
        "relay": {
            "host": "0.0.0.0",
            "port": DEFAULT_RELAY_PORT,
        },
        "security": {
            "tls_enabled": True,
            "token_expiry_seconds": 300,
            "max_auth_attempts": 5,
            "rate_limit_window": 60,
        },
        "remotes": {},
    }


def get_remote_config(name: str) -> dict[str, Any] | None:
    """Get configuration for a named remote."""
    config = load_config()
    return config.get("remotes", {}).get(name)


def add_remote_config(name: str, remote: dict[str, Any]) -> None:
    """Add or update a named remote configuration."""
    config = load_config()
    if "remotes" not in config:
        config["remotes"] = {}
    config["remotes"][name] = remote
    save_config(config)

