"""Bonjour/mDNS service registration and discovery for Terminal Bridge.

Uses the `zeroconf` library to register the remote agent as a discoverable
service on the local network, and to discover remote agents from the local bridge.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from terminal_bridge.config import DEFAULT_AGENT_PORT

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_terminal-bridge._tcp.local."
SERVICE_NAME_PREFIX = "Terminal Bridge"


def _get_zeroconf():
    """Import and return zeroconf module."""
    try:
        from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

        return Zeroconf, ServiceInfo, ServiceBrowser
    except ImportError:
        logger.warning("zeroconf not installed. LAN discovery unavailable.")
        return None, None, None


class BonjourService:
    """Register this machine as a Terminal Bridge agent on the local network."""

    def __init__(self, port: int = DEFAULT_AGENT_PORT, hostname: str | None = None) -> None:
        self.port = port
        self.hostname = hostname or socket.gethostname()
        self._zc = None
        self._info = None

    def register(self) -> bool:
        """Register the Bonjour service. Returns True if successful."""
        Zeroconf, ServiceInfo, _ = _get_zeroconf()
        if Zeroconf is None:
            return False

        try:
            self._zc = Zeroconf()

            # Get local IPs
            from terminal_bridge.setup.pairing import get_local_ips

            ips = get_local_ips()

            # Build addresses list
            import ipaddress

            addresses = []
            for ip in ips:
                try:
                    addresses.append(ipaddress.IPv4Address(ip).packed)
                except Exception:
                    pass

            service_name = f"{SERVICE_NAME_PREFIX} on {self.hostname}.{SERVICE_TYPE}"

            self._info = ServiceInfo(
                SERVICE_TYPE,
                service_name,
                addresses=addresses,
                port=self.port,
                properties={
                    b"version": b"1",
                    b"hostname": self.hostname.encode(),
                },
            )

            self._zc.register_service(self._info)
            logger.info("Registered Bonjour service: %s on port %d", service_name, self.port)
            return True

        except Exception as e:
            logger.warning("Failed to register Bonjour service: %s", e)
            return False

    def unregister(self) -> None:
        """Unregister the Bonjour service."""
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
            except Exception:
                pass
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None
            self._info = None


class BonjourDiscovery:
    """Discover Terminal Bridge agents on the local network."""

    def __init__(self) -> None:
        self._found: list[dict[str, Any]] = []

    async def discover(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Search for Terminal Bridge agents on the local network.

        Returns list of dicts with: hostname, ip, port
        """
        Zeroconf, ServiceInfo, ServiceBrowser = _get_zeroconf()
        if Zeroconf is None:
            return []

        self._found = []
        zc = Zeroconf()

        class Listener:
            def __init__(self, parent: BonjourDiscovery) -> None:
                self.parent = parent

            def add_service(self, zc_instance, type_: str, name: str) -> None:
                info = zc_instance.get_service_info(type_, name)
                if info:
                    addresses = info.parsed_addresses()
                    hostname = info.properties.get(b"hostname", b"unknown").decode()
                    for addr in addresses:
                        self.parent._found.append({
                            "hostname": hostname,
                            "ip": addr,
                            "port": info.port,
                            "name": name,
                        })

            def remove_service(self, zc_instance, type_: str, name: str) -> None:
                pass

            def update_service(self, zc_instance, type_: str, name: str) -> None:
                pass

        listener = Listener(self)
        browser = ServiceBrowser(zc, SERVICE_TYPE, listener)

        await asyncio.sleep(timeout)

        browser.cancel()
        zc.close()

        # Deduplicate
        seen = set()
        unique = []
        for entry in self._found:
            key = f"{entry['ip']}:{entry['port']}"
            if key not in seen:
                seen.add(key)
                unique.append(entry)

        return unique


async def discover_agents(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Convenience function to discover agents on the LAN."""
    discovery = BonjourDiscovery()
    return await discovery.discover(timeout=timeout)

