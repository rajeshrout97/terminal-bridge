"""Enterprise fleet management.

Centralized management of multiple remote Macs — registration, grouping,
health monitoring, and batch operations.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator


class MachineStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


@dataclass
class Machine:
    machine_id: str
    hostname: str
    host: str
    port: int = 9877
    display_name: str = ""
    group: str = "default"
    tags: list[str] = field(default_factory=list)
    status: MachineStatus = MachineStatus.UNKNOWN
    os_version: str = ""
    cpu_arch: str = ""
    last_seen: str = ""
    latency_ms: float = -1.0
    agent_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    registered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    machine_id    TEXT PRIMARY KEY,
    hostname      TEXT NOT NULL,
    host          TEXT NOT NULL,
    port          INTEGER NOT NULL DEFAULT 9877,
    display_name  TEXT NOT NULL DEFAULT '',
    grp           TEXT NOT NULL DEFAULT 'default',
    tags          TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'unknown',
    os_version    TEXT NOT NULL DEFAULT '',
    cpu_arch      TEXT NOT NULL DEFAULT '',
    last_seen     TEXT NOT NULL DEFAULT '',
    latency_ms    REAL NOT NULL DEFAULT -1.0,
    agent_version TEXT NOT NULL DEFAULT '',
    metadata      TEXT NOT NULL DEFAULT '{}',
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    name         TEXT PRIMARY KEY,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    status       TEXT NOT NULL,
    latency_ms   REAL NOT NULL DEFAULT -1.0,
    cpu_percent  REAL NOT NULL DEFAULT -1.0,
    memory_percent REAL NOT NULL DEFAULT -1.0,
    disk_percent REAL NOT NULL DEFAULT -1.0,
    detail       TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (machine_id) REFERENCES machines(machine_id)
);

CREATE INDEX IF NOT EXISTS idx_health_machine ON health_checks(machine_id);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health_checks(timestamp);
CREATE INDEX IF NOT EXISTS idx_machines_group ON machines(grp);
CREATE INDEX IF NOT EXISTS idx_machines_status ON machines(status);

INSERT OR IGNORE INTO groups (name, description, created_at)
    VALUES ('default', 'Default machine group', datetime('now'));
"""


class FleetManager:
    """Centralized management of multiple remote Macs."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            config_dir = Path.home() / ".config" / "terminal-bridge"
            config_dir.mkdir(parents=True, exist_ok=True)
            db_path = config_dir / "enterprise.db"
        self._db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── Machine Registration ──

    def register(
        self,
        hostname: str,
        host: str,
        port: int = 9877,
        *,
        display_name: str = "",
        group: str = "default",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Machine:
        machine_id = uuid.uuid4().hex[:12]
        machine = Machine(
            machine_id=machine_id,
            hostname=hostname,
            host=host,
            port=port,
            display_name=display_name or hostname,
            group=group,
            tags=tags or [],
            metadata=metadata or {},
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO machines "
                "(machine_id, hostname, host, port, display_name, grp, tags, status, os_version, "
                "cpu_arch, last_seen, latency_ms, agent_version, metadata, registered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    machine.machine_id, machine.hostname, machine.host, machine.port,
                    machine.display_name, machine.group, json.dumps(machine.tags),
                    machine.status.value, machine.os_version, machine.cpu_arch,
                    machine.last_seen, machine.latency_ms, machine.agent_version,
                    json.dumps(machine.metadata), machine.registered_at,
                ),
            )
        return machine

    def unregister(self, machine_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM machines WHERE machine_id = ?", (machine_id,))
            return cursor.rowcount > 0

    def get(self, machine_id: str) -> Machine | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM machines WHERE machine_id = ?", (machine_id,)).fetchone()
            if not row:
                return None
            return self._row_to_machine(row)

    def get_by_hostname(self, hostname: str) -> Machine | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM machines WHERE hostname = ?", (hostname,)).fetchone()
            if not row:
                return None
            return self._row_to_machine(row)

    def list_machines(
        self,
        *,
        group: str | None = None,
        status: MachineStatus | None = None,
        tag: str | None = None,
    ) -> list[Machine]:
        conditions: list[str] = []
        params: list[Any] = []
        if group:
            conditions.append("grp = ?")
            params.append(group)
        if status:
            conditions.append("status = ?")
            params.append(status.value)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM machines {where} ORDER BY display_name", params).fetchall()

        machines = [self._row_to_machine(r) for r in rows]
        if tag:
            machines = [m for m in machines if tag in m.tags]
        return machines

    def update(
        self,
        machine_id: str,
        *,
        display_name: str | None = None,
        group: str | None = None,
        tags: list[str] | None = None,
        host: str | None = None,
        port: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)
        if group is not None:
            updates.append("grp = ?")
            params.append(group)
        if tags is not None:
            updates.append("tags = ?")
            params.append(json.dumps(tags))
        if host is not None:
            updates.append("host = ?")
            params.append(host)
        if port is not None:
            updates.append("port = ?")
            params.append(port)
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))
        if not updates:
            return False
        params.append(machine_id)
        with self._conn() as conn:
            cursor = conn.execute(f"UPDATE machines SET {', '.join(updates)} WHERE machine_id = ?", params)
            return cursor.rowcount > 0

    def set_maintenance(self, machine_id: str, enabled: bool = True) -> bool:
        status = MachineStatus.MAINTENANCE if enabled else MachineStatus.UNKNOWN
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE machines SET status = ? WHERE machine_id = ?",
                (status.value, machine_id),
            )
            return cursor.rowcount > 0

    # ── Health Monitoring ──

    def record_health(
        self,
        machine_id: str,
        *,
        status: MachineStatus,
        latency_ms: float = -1.0,
        cpu_percent: float = -1.0,
        memory_percent: float = -1.0,
        disk_percent: float = -1.0,
        detail: dict[str, Any] | None = None,
        agent_version: str = "",
        os_version: str = "",
        cpu_arch: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO health_checks (machine_id, timestamp, status, latency_ms, cpu_percent, memory_percent, disk_percent, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (machine_id, now, status.value, latency_ms, cpu_percent, memory_percent, disk_percent, json.dumps(detail or {})),
            )
            updates = ["status = ?", "last_seen = ?", "latency_ms = ?"]
            params: list[Any] = [status.value, now, latency_ms]
            if agent_version:
                updates.append("agent_version = ?")
                params.append(agent_version)
            if os_version:
                updates.append("os_version = ?")
                params.append(os_version)
            if cpu_arch:
                updates.append("cpu_arch = ?")
                params.append(cpu_arch)
            params.append(machine_id)
            conn.execute(f"UPDATE machines SET {', '.join(updates)} WHERE machine_id = ?", params)

    def get_health_history(
        self, machine_id: str, *, hours: int = 24, limit: int = 200
    ) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM health_checks WHERE machine_id = ? AND timestamp >= ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (machine_id, since, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def purge_health(self, older_than_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM health_checks WHERE timestamp < ?", (cutoff,))
            return cursor.rowcount

    # ── Groups ──

    def create_group(self, name: str, description: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO groups (name, description, created_at) VALUES (?, ?, ?)",
                (name, description, datetime.now(timezone.utc).isoformat()),
            )

    def delete_group(self, name: str) -> bool:
        if name == "default":
            raise ValueError("Cannot delete the default group")
        with self._conn() as conn:
            conn.execute("UPDATE machines SET grp = 'default' WHERE grp = ?", (name,))
            cursor = conn.execute("DELETE FROM groups WHERE name = ?", (name,))
            return cursor.rowcount > 0

    def list_groups(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            groups = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
            result = []
            for g in groups:
                count = conn.execute("SELECT COUNT(*) FROM machines WHERE grp = ?", (g["name"],)).fetchone()[0]
                result.append({"name": g["name"], "description": g["description"], "machine_count": count})
            return result

    # ── Batch Operations ──

    async def batch_exec(
        self,
        command: str,
        *,
        group: str | None = None,
        machine_ids: list[str] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Execute a command on multiple machines. Returns results keyed by machine_id."""
        if machine_ids:
            machines = [m for m in self.list_machines() if m.machine_id in machine_ids]
        elif group:
            machines = self.list_machines(group=group)
        else:
            machines = self.list_machines()

        online = [m for m in machines if m.status == MachineStatus.ONLINE]

        async def _exec_one(machine: Machine) -> tuple[str, dict[str, Any]]:
            try:
                from terminal_bridge.local_bridge.client import exec_remote_command
                result = await exec_remote_command(
                    command, remote=machine.hostname, timeout=timeout
                )
                return machine.machine_id, {"success": True, "result": result, "hostname": machine.hostname}
            except Exception as e:
                return machine.machine_id, {"success": False, "error": str(e), "hostname": machine.hostname}

        tasks = [_exec_one(m) for m in online]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output: dict[str, Any] = {}
        for item in results:
            if isinstance(item, Exception):
                continue
            mid, res = item
            output[mid] = res

        return {
            "command": command,
            "total_machines": len(machines),
            "executed_on": len(online),
            "skipped_offline": len(machines) - len(online),
            "results": output,
        }

    # ── Fleet Overview ──

    def overview(self) -> dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
            by_status = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM machines GROUP BY status"
            ).fetchall()
            by_group = conn.execute(
                "SELECT grp, COUNT(*) as cnt FROM machines GROUP BY grp"
            ).fetchall()
            stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            stale = conn.execute(
                "SELECT COUNT(*) FROM machines WHERE last_seen < ? AND last_seen != '' AND status = 'online'",
                (stale_cutoff,),
            ).fetchone()[0]

        return {
            "total_machines": total,
            "by_status": {r["status"]: r["cnt"] for r in by_status},
            "by_group": {r["grp"]: r["cnt"] for r in by_group},
            "potentially_stale": stale,
        }

    @staticmethod
    def _row_to_machine(row: sqlite3.Row) -> Machine:
        return Machine(
            machine_id=row["machine_id"],
            hostname=row["hostname"],
            host=row["host"],
            port=row["port"],
            display_name=row["display_name"],
            group=row["grp"],
            tags=json.loads(row["tags"]),
            status=MachineStatus(row["status"]),
            os_version=row["os_version"],
            cpu_arch=row["cpu_arch"],
            last_seen=row["last_seen"],
            latency_ms=row["latency_ms"],
            agent_version=row["agent_version"],
            metadata=json.loads(row["metadata"]),
            registered_at=row["registered_at"],
        )
