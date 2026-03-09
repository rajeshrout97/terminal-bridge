"""Enterprise policy engine.

Enforces command allow/block lists, restricted paths, execution time windows,
and approval workflows. Policies can be scoped to users, roles, machines,
or groups.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator


class PolicyAction(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"
    LOG_ONLY = "log_only"


class PolicyScope(str, Enum):
    GLOBAL = "global"
    ROLE = "role"
    USER = "user"
    MACHINE = "machine"
    GROUP = "group"


@dataclass
class PolicyResult:
    allowed: bool
    policy_id: str | None = None
    policy_name: str | None = None
    action: PolicyAction = PolicyAction.ALLOW
    reason: str = ""
    requires_approval: bool = False


@dataclass
class Policy:
    policy_id: str
    name: str
    description: str = ""
    action: PolicyAction = PolicyAction.BLOCK
    priority: int = 100
    is_active: bool = True

    scope: PolicyScope = PolicyScope.GLOBAL
    scope_value: str = ""

    command_patterns: list[str] = field(default_factory=list)
    blocked_commands: list[str] = field(default_factory=list)
    allowed_commands: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    max_timeout: int = 0
    require_session: bool = False

    time_window_start: str = ""
    time_window_end: str = ""
    allowed_days: list[int] = field(default_factory=list)

    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    policy_id        TEXT PRIMARY KEY,
    name             TEXT UNIQUE NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    action           TEXT NOT NULL DEFAULT 'block',
    priority         INTEGER NOT NULL DEFAULT 100,
    is_active        INTEGER NOT NULL DEFAULT 1,
    scope            TEXT NOT NULL DEFAULT 'global',
    scope_value      TEXT NOT NULL DEFAULT '',
    command_patterns TEXT NOT NULL DEFAULT '[]',
    blocked_commands TEXT NOT NULL DEFAULT '[]',
    allowed_commands TEXT NOT NULL DEFAULT '[]',
    blocked_paths    TEXT NOT NULL DEFAULT '[]',
    max_timeout      INTEGER NOT NULL DEFAULT 0,
    require_session  INTEGER NOT NULL DEFAULT 0,
    time_window_start TEXT NOT NULL DEFAULT '',
    time_window_end   TEXT NOT NULL DEFAULT '',
    allowed_days     TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policies_scope ON policies(scope, scope_value);
CREATE INDEX IF NOT EXISTS idx_policies_active ON policies(is_active);

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id    TEXT PRIMARY KEY,
    policy_id     TEXT NOT NULL,
    actor         TEXT NOT NULL,
    remote        TEXT NOT NULL,
    command       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    reviewer      TEXT NOT NULL DEFAULT '',
    review_note   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    reviewed_at   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_requests(status);
"""


DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs.*",
    "dd if=*of=/dev/*",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "curl*|*sh",
    "wget*|*sh",
    "sudo rm -rf",
    "> /dev/sda",
    "shutdown",
    "reboot",
    "halt",
    "init 0",
    "init 6",
]


class PolicyEngine:
    """Evaluates commands against configured policies."""

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

    # ── Policy CRUD ──

    def create_policy(self, policy: Policy) -> Policy:
        if not policy.policy_id:
            policy.policy_id = uuid.uuid4().hex[:12]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO policies "
                "(policy_id, name, description, action, priority, is_active, scope, scope_value, "
                "command_patterns, blocked_commands, allowed_commands, blocked_paths, "
                "max_timeout, require_session, time_window_start, time_window_end, allowed_days, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    policy.policy_id, policy.name, policy.description, policy.action.value,
                    policy.priority, int(policy.is_active), policy.scope.value, policy.scope_value,
                    json.dumps(policy.command_patterns), json.dumps(policy.blocked_commands),
                    json.dumps(policy.allowed_commands), json.dumps(policy.blocked_paths),
                    policy.max_timeout, int(policy.require_session),
                    policy.time_window_start, policy.time_window_end,
                    json.dumps(policy.allowed_days), policy.created_at,
                ),
            )
        return policy

    def get_policy(self, policy_id: str) -> Policy | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM policies WHERE policy_id = ?", (policy_id,)).fetchone()
            if not row:
                return None
            return self._row_to_policy(row)

    def list_policies(self, *, active_only: bool = True) -> list[Policy]:
        with self._conn() as conn:
            sql = "SELECT * FROM policies"
            if active_only:
                sql += " WHERE is_active = 1"
            sql += " ORDER BY priority ASC"
            rows = conn.execute(sql).fetchall()
            return [self._row_to_policy(r) for r in rows]

    def update_policy(self, policy_id: str, **kwargs: Any) -> bool:
        field_map = {
            "name": "name", "description": "description", "action": "action",
            "priority": "priority", "is_active": "is_active",
            "command_patterns": "command_patterns", "blocked_commands": "blocked_commands",
            "allowed_commands": "allowed_commands", "blocked_paths": "blocked_paths",
            "max_timeout": "max_timeout",
        }
        updates: list[str] = []
        params: list[Any] = []
        for key, val in kwargs.items():
            if key not in field_map:
                continue
            col = field_map[key]
            if isinstance(val, (list, dict)):
                val = json.dumps(val)
            elif isinstance(val, bool):
                val = int(val)
            elif isinstance(val, Enum):
                val = val.value
            updates.append(f"{col} = ?")
            params.append(val)
        if not updates:
            return False
        params.append(policy_id)
        with self._conn() as conn:
            cursor = conn.execute(f"UPDATE policies SET {', '.join(updates)} WHERE policy_id = ?", params)
            return cursor.rowcount > 0

    def delete_policy(self, policy_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM policies WHERE policy_id = ?", (policy_id,))
            return cursor.rowcount > 0

    # ── Policy Evaluation ──

    def evaluate(
        self,
        command: str,
        *,
        actor: str = "",
        actor_role: str = "",
        remote: str = "",
        remote_group: str = "",
        timeout: int = 30,
    ) -> PolicyResult:
        """Evaluate a command against all active policies. Returns the first matching result."""
        if self._matches_dangerous(command):
            return PolicyResult(
                allowed=False,
                action=PolicyAction.BLOCK,
                reason=f"Command matches a dangerous pattern and is blocked by default",
            )

        policies = self._get_applicable_policies(
            actor=actor, actor_role=actor_role, remote=remote, remote_group=remote_group
        )

        for policy in policies:
            result = self._check_policy(policy, command, timeout=timeout)
            if result is not None:
                return result

        return PolicyResult(allowed=True, action=PolicyAction.ALLOW, reason="No policy matched")

    def _get_applicable_policies(
        self, *, actor: str, actor_role: str, remote: str, remote_group: str
    ) -> list[Policy]:
        all_policies = self.list_policies(active_only=True)
        applicable = []
        for p in all_policies:
            if p.scope == PolicyScope.GLOBAL:
                applicable.append(p)
            elif p.scope == PolicyScope.USER and p.scope_value == actor:
                applicable.append(p)
            elif p.scope == PolicyScope.ROLE and p.scope_value == actor_role:
                applicable.append(p)
            elif p.scope == PolicyScope.MACHINE and p.scope_value == remote:
                applicable.append(p)
            elif p.scope == PolicyScope.GROUP and p.scope_value == remote_group:
                applicable.append(p)
        return sorted(applicable, key=lambda p: p.priority)

    def _check_policy(self, policy: Policy, command: str, *, timeout: int = 30) -> PolicyResult | None:
        matched = False

        if policy.blocked_commands:
            for pattern in policy.blocked_commands:
                if self._command_matches(command, pattern):
                    matched = True
                    break

        if not matched and policy.command_patterns:
            for pattern in policy.command_patterns:
                if self._command_matches(command, pattern):
                    matched = True
                    break

        if not matched and policy.blocked_paths:
            for path_pattern in policy.blocked_paths:
                if path_pattern in command:
                    matched = True
                    break

        if not matched and policy.allowed_commands:
            is_allowed = any(self._command_matches(command, p) for p in policy.allowed_commands)
            if not is_allowed:
                matched = True

        if not matched and policy.max_timeout > 0 and timeout > policy.max_timeout:
            matched = True

        if not matched and policy.time_window_start and policy.time_window_end:
            if not self._in_time_window(policy):
                matched = True

        if not matched:
            return None

        if policy.action == PolicyAction.BLOCK:
            return PolicyResult(
                allowed=False,
                policy_id=policy.policy_id,
                policy_name=policy.name,
                action=PolicyAction.BLOCK,
                reason=f"Blocked by policy: {policy.name}",
            )
        elif policy.action == PolicyAction.REQUIRE_APPROVAL:
            return PolicyResult(
                allowed=False,
                policy_id=policy.policy_id,
                policy_name=policy.name,
                action=PolicyAction.REQUIRE_APPROVAL,
                reason=f"Requires approval per policy: {policy.name}",
                requires_approval=True,
            )
        elif policy.action == PolicyAction.LOG_ONLY:
            return PolicyResult(
                allowed=True,
                policy_id=policy.policy_id,
                policy_name=policy.name,
                action=PolicyAction.LOG_ONLY,
                reason=f"Logged per policy: {policy.name}",
            )

        return None

    @staticmethod
    def _command_matches(command: str, pattern: str) -> bool:
        cmd_lower = command.lower().strip()
        pat_lower = pattern.lower().strip()
        if fnmatch.fnmatch(cmd_lower, pat_lower):
            return True
        if cmd_lower.startswith(pat_lower):
            return True
        try:
            if re.search(pat_lower, cmd_lower):
                return True
        except re.error:
            pass
        return False

    @staticmethod
    def _matches_dangerous(command: str) -> bool:
        cmd_lower = command.lower().strip()
        for pattern in DANGEROUS_PATTERNS:
            if fnmatch.fnmatch(cmd_lower, pattern.lower()):
                return True
            if pattern.lower() in cmd_lower:
                return True
        return False

    @staticmethod
    def _in_time_window(policy: Policy) -> bool:
        now = datetime.now(timezone.utc)
        if policy.allowed_days and now.weekday() not in policy.allowed_days:
            return False
        if policy.time_window_start and policy.time_window_end:
            current_time = now.strftime("%H:%M")
            if not (policy.time_window_start <= current_time <= policy.time_window_end):
                return False
        return True

    # ── Approval Workflow ──

    def create_approval_request(
        self, policy_id: str, actor: str, remote: str, command: str
    ) -> str:
        request_id = uuid.uuid4().hex[:12]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO approval_requests (request_id, policy_id, actor, remote, command, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, policy_id, actor, remote, command, datetime.now(timezone.utc).isoformat()),
            )
        return request_id

    def review_approval(self, request_id: str, *, approved: bool, reviewer: str, note: str = "") -> bool:
        status = "approved" if approved else "rejected"
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE approval_requests SET status = ?, reviewer = ?, review_note = ?, reviewed_at = ? "
                "WHERE request_id = ? AND status = 'pending'",
                (status, reviewer, note, datetime.now(timezone.utc).isoformat(), request_id),
            )
            return cursor.rowcount > 0

    def get_pending_approvals(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def is_approved(self, request_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM approval_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return row is not None and row["status"] == "approved"

    # ── Preset Policies ──

    def install_defaults(self) -> list[str]:
        """Install sensible default policies. Returns list of created policy IDs."""
        defaults = [
            Policy(
                policy_id="", name="block-destructive",
                description="Block destructive commands like rm -rf /, format disk, etc.",
                action=PolicyAction.BLOCK, priority=1,
                blocked_commands=DANGEROUS_PATTERNS,
            ),
            Policy(
                policy_id="", name="block-network-exfil",
                description="Block piping output to external URLs",
                action=PolicyAction.BLOCK, priority=10,
                command_patterns=["*curl*|*", "*wget*-O*-*|*", "*nc *"],
            ),
            Policy(
                policy_id="", name="block-sensitive-paths",
                description="Block access to sensitive paths",
                action=PolicyAction.BLOCK, priority=20,
                blocked_paths=[
                    "/etc/shadow", "/etc/passwd", "~/.ssh/id_",
                    "~/.gnupg/", "/var/db/dslocal/",
                ],
            ),
            Policy(
                policy_id="", name="approve-sudo",
                description="Require approval for sudo commands",
                action=PolicyAction.REQUIRE_APPROVAL, priority=50,
                command_patterns=["sudo *"],
            ),
            Policy(
                policy_id="", name="log-package-installs",
                description="Log all package install commands",
                action=PolicyAction.LOG_ONLY, priority=80,
                command_patterns=["*brew install*", "*pip install*", "*npm install*", "*apt install*"],
            ),
        ]
        ids = []
        for policy in defaults:
            try:
                created = self.create_policy(policy)
                ids.append(created.policy_id)
            except sqlite3.IntegrityError:
                pass
        return ids

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM policies WHERE is_active = 1").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'").fetchone()[0]
            by_action = conn.execute(
                "SELECT action, COUNT(*) as cnt FROM policies WHERE is_active = 1 GROUP BY action"
            ).fetchall()
        return {
            "total_policies": total,
            "active_policies": active,
            "pending_approvals": pending,
            "by_action": {r["action"]: r["cnt"] for r in by_action},
        }

    @staticmethod
    def _row_to_policy(row: sqlite3.Row) -> Policy:
        return Policy(
            policy_id=row["policy_id"], name=row["name"], description=row["description"],
            action=PolicyAction(row["action"]), priority=row["priority"],
            is_active=bool(row["is_active"]), scope=PolicyScope(row["scope"]),
            scope_value=row["scope_value"],
            command_patterns=json.loads(row["command_patterns"]),
            blocked_commands=json.loads(row["blocked_commands"]),
            allowed_commands=json.loads(row["allowed_commands"]),
            blocked_paths=json.loads(row["blocked_paths"]),
            max_timeout=row["max_timeout"], require_session=bool(row["require_session"]),
            time_window_start=row["time_window_start"], time_window_end=row["time_window_end"],
            allowed_days=json.loads(row["allowed_days"]), created_at=row["created_at"],
        )
