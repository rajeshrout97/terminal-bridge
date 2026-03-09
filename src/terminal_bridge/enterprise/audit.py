"""Enterprise audit logging system.

Provides a tamper-evident, queryable audit trail of every operation performed
through Terminal Bridge. Backed by SQLite with support for log export,
retention policies, and real-time streaming.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator


class AuditAction(str, Enum):
    COMMAND_EXEC = "command.exec"
    COMMAND_EXEC_RESULT = "command.exec.result"
    SESSION_CREATE = "session.create"
    SESSION_INPUT = "session.input"
    SESSION_END = "session.end"
    FILE_PUSH = "file.push"
    FILE_PULL = "file.pull"
    AUTH_SUCCESS = "auth.success"
    AUTH_FAILURE = "auth.failure"
    CONNECTION_OPEN = "connection.open"
    CONNECTION_CLOSE = "connection.close"
    POLICY_BLOCK = "policy.block"
    RBAC_DENY = "rbac.deny"
    ADMIN_ACTION = "admin.action"
    FLEET_HEALTH = "fleet.health"


class AuditSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class AuditEntry:
    action: AuditAction
    actor: str
    remote: str
    detail: dict[str, Any] = field(default_factory=dict)
    severity: AuditSeverity = AuditSeverity.INFO
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    prev_hash: str = ""
    entry_hash: str = ""

    def compute_hash(self, prev_hash: str = "") -> str:
        self.prev_hash = prev_hash
        payload = f"{self.entry_id}|{self.timestamp}|{self.action}|{self.actor}|{self.remote}|{json.dumps(self.detail, sort_keys=True)}|{prev_hash}"
        self.entry_hash = hashlib.sha256(payload.encode()).hexdigest()
        return self.entry_hash


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT UNIQUE NOT NULL,
    timestamp   TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    remote      TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    detail      TEXT NOT NULL DEFAULT '{}',
    prev_hash   TEXT NOT NULL DEFAULT '',
    entry_hash  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_remote ON audit_log(remote);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_log(severity);
"""


class AuditLog:
    """Tamper-evident audit log backed by SQLite with hash chaining."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            config_dir = Path.home() / ".config" / "terminal-bridge"
            config_dir.mkdir(parents=True, exist_ok=True)
            db_path = config_dir / "audit.db"
        self._db_path = Path(db_path)
        self._last_hash = ""
        self._listeners: list[asyncio.Queue] = []
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                self._last_hash = row[0]

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def log(
        self,
        action: AuditAction,
        actor: str,
        remote: str,
        detail: dict[str, Any] | None = None,
        severity: AuditSeverity = AuditSeverity.INFO,
    ) -> AuditEntry:
        entry = AuditEntry(
            action=action,
            actor=actor,
            remote=remote,
            detail=detail or {},
            severity=severity,
        )
        entry.compute_hash(self._last_hash)
        self._last_hash = entry.entry_hash

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (entry_id, timestamp, action, actor, remote, severity, detail, prev_hash, entry_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.entry_id,
                    entry.timestamp,
                    entry.action.value,
                    entry.actor,
                    entry.remote,
                    entry.severity.value,
                    json.dumps(entry.detail),
                    entry.prev_hash,
                    entry.entry_hash,
                    time.time(),
                ),
            )

        for q in self._listeners:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

        return entry

    def query(
        self,
        *,
        action: AuditAction | None = None,
        actor: str | None = None,
        remote: str | None = None,
        severity: AuditSeverity | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        if action:
            conditions.append("action = ?")
            params.append(action.value)
        if actor:
            conditions.append("actor = ?")
            params.append(actor)
        if remote:
            conditions.append("remote = ?")
            params.append(remote)
        if severity:
            conditions.append("severity = ?")
            params.append(severity.value)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            conditions.append("timestamp <= ?")
            params.append(until.isoformat())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def count(
        self,
        *,
        action: AuditAction | None = None,
        actor: str | None = None,
        remote: str | None = None,
        since: datetime | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if action:
            conditions.append("action = ?")
            params.append(action.value)
        if actor:
            conditions.append("actor = ?")
            params.append(actor)
        if remote:
            conditions.append("remote = ?")
            params.append(remote)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._conn() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()
            return row[0]

    def verify_chain(self, limit: int = 1000) -> tuple[bool, int]:
        """Verify the hash chain integrity. Returns (valid, entries_checked)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT entry_id, timestamp, action, actor, remote, detail, prev_hash, entry_hash "
                "FROM audit_log ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()

        prev_hash = ""
        for i, row in enumerate(rows):
            entry = AuditEntry(
                entry_id=row["entry_id"],
                timestamp=row["timestamp"],
                action=AuditAction(row["action"]),
                actor=row["actor"],
                remote=row["remote"],
                detail=json.loads(row["detail"]),
            )
            expected = entry.compute_hash(prev_hash)
            if expected != row["entry_hash"] or prev_hash != row["prev_hash"]:
                return False, i
            prev_hash = expected

        return True, len(rows)

    def export_json(self, *, since: datetime | None = None, limit: int = 10000) -> str:
        entries = self.query(since=since, limit=limit)
        return json.dumps(entries, indent=2, default=str)

    def purge(self, older_than_days: int = 90) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < ?", (cutoff.isoformat(),)
            )
            return cursor.rowcount

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners = [l for l in self._listeners if l is not q]

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            actions = conn.execute(
                "SELECT action, COUNT(*) as cnt FROM audit_log GROUP BY action ORDER BY cnt DESC"
            ).fetchall()
            actors = conn.execute(
                "SELECT actor, COUNT(*) as cnt FROM audit_log GROUP BY actor ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
            recent_critical = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE severity = 'critical' AND timestamp > ?",
                ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
            ).fetchone()[0]

        return {
            "total_entries": total,
            "actions": {r["action"]: r["cnt"] for r in actions},
            "top_actors": {r["actor"]: r["cnt"] for r in actors},
            "critical_last_24h": recent_critical,
            "db_path": str(self._db_path),
        }
