"""WebSocket relay server for Terminal Bridge.

Enables internet connections between Macs that are behind NAT/firewalls.
Both the remote agent and local bridge connect outbound to this relay,
avoiding the need for port forwarding.

Architecture:
    1. Remote agent connects and registers a room with an auth token
    2. Local bridge connects and joins the room with the same token
    3. Relay pairs them and forwards all messages bidirectionally

Usage:
    tbridge relay start --port 9878
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time

import websockets.asyncio.server
import websockets.exceptions

from terminal_bridge.config import DEFAULT_RELAY_PORT
from terminal_bridge.protocol.messages import (
    Message,
    MessageType,
    RelayError,
    RelayJoin,
    RelayPaired,
    RelayRegister,
    make_message,
    parse_payload,
)
from terminal_bridge.version import __version__

logger = logging.getLogger(__name__)


class RelayRoom:
    """A paired connection between a remote agent and local bridge."""

    def __init__(self, room_id: str, auth_token: str) -> None:
        self.room_id = room_id
        self.auth_token = auth_token
        self.agent: websockets.asyncio.server.ServerConnection | None = None
        self.bridge: websockets.asyncio.server.ServerConnection | None = None
        self.created_at = time.time()
        self.last_activity = time.time()

    @property
    def is_paired(self) -> bool:
        return self.agent is not None and self.bridge is not None

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_activity


class RelayServer:
    """Stateless WebSocket relay server with room-based pairing."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_RELAY_PORT) -> None:
        self.host = host
        self.port = port
        self._rooms: dict[str, RelayRoom] = {}
        self._client_rooms: dict[int, str] = {}  # websocket id -> room_id
        self._server: websockets.asyncio.server.Server | None = None

    async def start(self) -> None:
        """Start the relay server."""
        self._server = await websockets.asyncio.server.serve(
            self._handle_client,
            self.host,
            self.port,
        )

        # Start cleanup task
        asyncio.create_task(self._cleanup_loop())

        logger.info("Relay server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Stop the relay server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Relay server stopped.")

    async def _handle_client(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
    ) -> None:
        """Handle a new client connection."""
        client_id = id(websocket)
        remote_addr = websocket.remote_address
        logger.info("New relay connection from %s", remote_addr)

        try:
            # First message determines if this is an agent (register) or bridge (join)
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            msg = Message.from_json(raw)

            if msg.type == MessageType.RELAY_REGISTER:
                await self._handle_register(websocket, msg, client_id)
            elif msg.type == MessageType.RELAY_JOIN:
                await self._handle_join(websocket, msg, client_id)
            else:
                error = make_message(
                    MessageType.RELAY_ERROR,
                    RelayError(code="invalid_first_message", message="Expected register or join"),
                )
                await websocket.send(error.to_json())
                return

            # Now forward messages between paired connections
            await self._forward_loop(websocket, client_id)

        except websockets.exceptions.ConnectionClosed:
            logger.info("Relay client %s disconnected", remote_addr)
        except asyncio.TimeoutError:
            logger.warning("Relay client %s timed out", remote_addr)
        except Exception as e:
            logger.error("Relay error for %s: %s", remote_addr, e)
        finally:
            # Clean up
            room_id = self._client_rooms.pop(client_id, None)
            if room_id and room_id in self._rooms:
                room = self._rooms[room_id]
                if room.agent and id(room.agent) == client_id:
                    room.agent = None
                if room.bridge and id(room.bridge) == client_id:
                    room.bridge = None
                # If both sides disconnected, remove the room
                if room.agent is None and room.bridge is None:
                    self._rooms.pop(room_id, None)
                    logger.info("Room %s removed", room_id)

    async def _handle_register(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: int,
    ) -> None:
        """Handle an agent registering a room."""
        reg = parse_payload(msg, RelayRegister)

        if reg.room_id in self._rooms:
            room = self._rooms[reg.room_id]
            if room.auth_token != reg.auth_token:
                error = make_message(
                    MessageType.RELAY_ERROR,
                    RelayError(code="auth_failed", message="Invalid token for room"),
                )
                await websocket.send(error.to_json())
                return
            room.agent = websocket
        else:
            room = RelayRoom(room_id=reg.room_id, auth_token=reg.auth_token)
            room.agent = websocket
            self._rooms[reg.room_id] = room

        self._client_rooms[client_id] = reg.room_id
        logger.info("Agent registered room %s", reg.room_id)

        # If bridge is already waiting, notify both
        if room.is_paired:
            await self._notify_paired(room)

    async def _handle_join(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        msg: Message,
        client_id: int,
    ) -> None:
        """Handle a bridge joining a room."""
        join = parse_payload(msg, RelayJoin)

        room = self._rooms.get(join.room_id)
        if room is None:
            error = make_message(
                MessageType.RELAY_ERROR,
                RelayError(code="room_not_found", message=f"Room {join.room_id} not found"),
            )
            await websocket.send(error.to_json())
            return

        if room.auth_token != join.auth_token:
            error = make_message(
                MessageType.RELAY_ERROR,
                RelayError(code="auth_failed", message="Invalid token for room"),
            )
            await websocket.send(error.to_json())
            return

        room.bridge = websocket
        self._client_rooms[client_id] = join.room_id
        logger.info("Bridge joined room %s", join.room_id)

        # If agent is already here, notify both
        if room.is_paired:
            await self._notify_paired(room)

    async def _notify_paired(self, room: RelayRoom) -> None:
        """Notify both sides that they are paired."""
        paired_msg = make_message(
            MessageType.RELAY_PAIRED,
            RelayPaired(room_id=room.room_id, peer_hostname="paired"),
        )
        try:
            if room.agent:
                await room.agent.send(paired_msg.to_json())
            if room.bridge:
                await room.bridge.send(paired_msg.to_json())
        except Exception:
            pass
        logger.info("Room %s paired!", room.room_id)

    async def _forward_loop(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        client_id: int,
    ) -> None:
        """Forward messages from this client to its peer."""
        async for raw in websocket:
            room_id = self._client_rooms.get(client_id)
            if not room_id:
                continue

            room = self._rooms.get(room_id)
            if not room:
                continue

            room.last_activity = time.time()

            # Determine peer
            if room.agent and id(room.agent) == client_id:
                peer = room.bridge
            elif room.bridge and id(room.bridge) == client_id:
                peer = room.agent
            else:
                continue

            if peer:
                try:
                    await peer.send(raw)
                except Exception:
                    pass

    async def _cleanup_loop(self) -> None:
        """Periodically clean up stale rooms."""
        while True:
            await asyncio.sleep(60)
            stale = [
                rid
                for rid, room in self._rooms.items()
                if room.idle_seconds > 3600 and not room.is_paired
            ]
            for rid in stale:
                room = self._rooms.pop(rid, None)
                if room:
                    logger.info("Cleaned up stale room %s", rid)


async def start_relay(port: int = DEFAULT_RELAY_PORT, foreground: bool = True) -> None:
    """Start the relay server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    server = RelayServer(port=port)
    await server.start()

    from rich.console import Console

    console = Console()
    console.print(f"\n[bold green]Terminal Bridge Relay v{__version__}[/bold green]")
    console.print(f"Listening on [cyan]0.0.0.0:{port}[/cyan]")
    console.print("Press Ctrl+C to stop.\n")

    stop_event = asyncio.Event()

    def handle_signal() -> None:
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()
    await server.stop()

