"""Tests for configuration management."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

from terminal_bridge.config import default_config, load_config, save_config


def test_default_config_structure():
    """Test the default config has all expected keys."""
    config = default_config()
    assert "version" in config
    assert "agent" in config
    assert "bridge" in config
    assert "relay" in config
    assert "security" in config
    assert "remotes" in config


def test_default_config_agent_values():
    """Test default agent config values."""
    config = default_config()
    agent = config["agent"]
    assert agent["port"] == 9877
    assert agent["max_sessions"] == 10
    assert agent["enable_bonjour"] is True


def test_default_config_security_values():
    """Test default security config values."""
    config = default_config()
    sec = config["security"]
    assert sec["tls_enabled"] is True
    assert sec["max_auth_attempts"] == 5


def test_save_and_load_config():
    """Test config save and load roundtrip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "config.yaml"

        config = default_config()
        config["agent"]["port"] = 12345

        with patch("terminal_bridge.config.CONFIG_FILE", config_file):
            with patch("terminal_bridge.config.CONFIG_DIR", Path(tmpdir)):
                save_config(config)
                loaded = load_config()
                assert loaded["agent"]["port"] == 12345


def test_config_file_permissions():
    """Test that config file has restricted permissions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "config.yaml"

        with patch("terminal_bridge.config.CONFIG_FILE", config_file):
            with patch("terminal_bridge.config.CONFIG_DIR", Path(tmpdir)):
                save_config(default_config())
                mode = oct(config_file.stat().st_mode)[-3:]
                assert mode == "600"

