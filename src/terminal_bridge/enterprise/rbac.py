"""Enterprise role-based access control (RBAC).

Manages users, roles, API keys, and permission checks. Supports three built-in
roles (admin, operator, viewer) and custom roles with fine-grained permissions.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator


class Permission(str, Enum):
    COMMAND_EXEC = "command.exec"
    SESSION_MANAGE = "session.manage"
    FILE_TRANSFER = "file.transfer"
    FLEET_VIEW = "fleet.view"
    FLEET_MANAGE = "fleet.manage"
    AUDIT_VIEW = "audit.view"
    AUDIT_EXPORT = "audit.export"
    POLICY_VIEW = "policy.view"
    POLICY_MANAGE = "policy.manage"
    USER_VIEW = "user.view"
    USER_MANAGE = "user.manage"
    ADMIN_ALL = "admin.*"


BUILTIN_ROLES: dict[str, set[Permission]] = {
    "admin": {Permission.ADMIN_ALL},
    "operator": {
        Permission.COMMAND_EXEC,
        Permission.SESSION_MANAGE,
        Permission.FILE_TRANSFER,
        Permission.FLEET_VIEW,
        Permission.AUDIT_VIEW,
        Permission.POLICY_VIEW,
        Permission.USER_VIEW,
    },
    "viewer": {
        Permission.FLEET_VIEW,
        Permission.AUDIT_VIEW,
        Permission.POLICY_VIEW,
    },
}


@dataclass
class User:
    user_id: str
    name: str
    email: str
    role: str
    api_key_hash: str = ""
    is_active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_seen: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    api_key_hash  TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_seen     TEXT NOT NULL DEFAULT '',
    metadata      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS custom_roles (
    role_name    TEXT PRIMARY KEY,
    permissions  TEXT NOT NULL DEFAULT '[]',
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash     TEXT PRIMARY KEY,
    key_prefix   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    permissions  TEXT NOT NULL DEFAULT '[]',
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_used    TEXT NOT NULL DEFAULT '',
    expires_at   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
"""


class RBACManager:
    """Manages users, roles, API keys, and permission enforcement."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            config_dir = Path.home() / ".config" / "terminal-bridge"
            config_dir.mkdir(parents=True, exist_ok=True)
            db_path = config_dir / "enterprise.db"
        self._db_path = Path(db_path)
        self._custom_roles: dict[str, set[Permission]] = {}
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            rows = conn.execute("SELECT role_name, permissions FROM custom_roles").fetchall()
            for row in rows:
                perms = json.loads(row["permissions"])
                self._custom_roles[row["role_name"]] = {Permission(p) for p in perms}

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _hash_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    # ── User Management ──

    def create_user(self, name: str, email: str, role: str = "viewer") -> tuple[User, str]:
        """Create a user and return (user, api_key). The api_key is shown only once."""
        if role not in BUILTIN_ROLES and role not in self._custom_roles:
            raise ValueError(f"Unknown role: {role}")

        user_id = uuid.uuid4().hex[:12]
        api_key = f"tb_{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(api_key)

        user = User(
            user_id=user_id,
            name=name,
            email=email,
            role=role,
            api_key_hash=key_hash,
        )

        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (user_id, name, email, role, api_key_hash, is_active, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user.user_id, user.name, user.email, user.role, key_hash, 1, user.created_at, "{}"),
            )
            conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, user_id, label, permissions, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_hash, api_key[:10], user_id, "default", "[]", 1, user.created_at),
            )

        return user, api_key

    def get_user(self, user_id: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                return None
            return User(**{k: json.loads(row[k]) if k == "metadata" else row[k] for k in row.keys()})

    def get_user_by_email(self, email: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                return None
            return User(
                user_id=row["user_id"], name=row["name"], email=row["email"],
                role=row["role"], api_key_hash=row["api_key_hash"],
                is_active=bool(row["is_active"]), created_at=row["created_at"],
                last_seen=row["last_seen"], metadata=json.loads(row["metadata"]),
            )

    def list_users(self, include_inactive: bool = False) -> list[User]:
        with self._conn() as conn:
            sql = "SELECT * FROM users" if include_inactive else "SELECT * FROM users WHERE is_active = 1"
            rows = conn.execute(sql).fetchall()
            return [
                User(
                    user_id=r["user_id"], name=r["name"], email=r["email"],
                    role=r["role"], api_key_hash=r["api_key_hash"],
                    is_active=bool(r["is_active"]), created_at=r["created_at"],
                    last_seen=r["last_seen"], metadata=json.loads(r["metadata"]),
                )
                for r in rows
            ]

    def update_user(self, user_id: str, *, name: str | None = None, role: str | None = None, is_active: bool | None = None) -> bool:
        updates: list[str] = []
        params: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if role is not None:
            if role not in BUILTIN_ROLES and role not in self._custom_roles:
                raise ValueError(f"Unknown role: {role}")
            updates.append("role = ?")
            params.append(role)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(int(is_active))
        if not updates:
            return False
        params.append(user_id)
        with self._conn() as conn:
            cursor = conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
            return cursor.rowcount > 0

    def deactivate_user(self, user_id: str) -> bool:
        return self.update_user(user_id, is_active=False)

    # ── API Key Management ──

    def create_api_key(
        self,
        user_id: str,
        label: str = "",
        permissions: list[Permission] | None = None,
        expires_in_days: int | None = None,
    ) -> str:
        """Generate a new API key for a user. Returns the key (shown only once)."""
        api_key = f"tb_{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(api_key)
        now = datetime.now(timezone.utc).isoformat()
        expires = ""
        if expires_in_days:
            from datetime import timedelta
            expires = (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()

        perms = json.dumps([p.value for p in (permissions or [])])
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, user_id, label, permissions, is_active, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key_hash, api_key[:10], user_id, label, perms, 1, now, expires),
            )
        return api_key

    def authenticate(self, api_key: str) -> User | None:
        """Authenticate an API key and return the associated user, or None."""
        key_hash = self._hash_key(api_key)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT k.user_id, k.expires_at, k.is_active as key_active, u.* "
                "FROM api_keys k JOIN users u ON k.user_id = u.user_id "
                "WHERE k.key_hash = ?",
                (key_hash,),
            ).fetchone()
            if not row:
                return None
            if not row["key_active"] or not row["is_active"]:
                return None
            if row["expires_at"]:
                if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                    return None

            now = datetime.now(timezone.utc).isoformat()
            conn.execute("UPDATE api_keys SET last_used = ? WHERE key_hash = ?", (now, key_hash))
            conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (now, row["user_id"]))

            return User(
                user_id=row["user_id"], name=row["name"], email=row["email"],
                role=row["role"], api_key_hash=row["api_key_hash"],
                is_active=bool(row["is_active"]), created_at=row["created_at"],
                last_seen=now, metadata=json.loads(row["metadata"]),
            )

    def revoke_api_key(self, api_key: str) -> bool:
        key_hash = self._hash_key(api_key)
        with self._conn() as conn:
            cursor = conn.execute("UPDATE api_keys SET is_active = 0 WHERE key_hash = ?", (key_hash,))
            return cursor.rowcount > 0

    def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key_prefix, label, is_active, created_at, last_used, expires_at "
                "FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Permission Checks ──

    def get_permissions(self, role: str) -> set[Permission]:
        if role in BUILTIN_ROLES:
            return BUILTIN_ROLES[role]
        return self._custom_roles.get(role, set())

    def has_permission(self, user: User, permission: Permission) -> bool:
        if not user.is_active:
            return False
        perms = self.get_permissions(user.role)
        return Permission.ADMIN_ALL in perms or permission in perms

    def check_permission(self, user: User, permission: Permission) -> None:
        """Raise PermissionError if the user lacks the permission."""
        if not self.has_permission(user, permission):
            raise PermissionError(
                f"User '{user.name}' (role={user.role}) lacks permission: {permission.value}"
            )

    # ── Custom Roles ──

    def create_role(self, name: str, permissions: list[Permission], description: str = "") -> None:
        if name in BUILTIN_ROLES:
            raise ValueError(f"Cannot override built-in role: {name}")
        perm_set = set(permissions)
        self._custom_roles[name] = perm_set
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO custom_roles (role_name, permissions, description, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, json.dumps([p.value for p in perm_set]), description, datetime.now(timezone.utc).isoformat()),
            )

    def delete_role(self, name: str) -> bool:
        if name in BUILTIN_ROLES:
            raise ValueError(f"Cannot delete built-in role: {name}")
        self._custom_roles.pop(name, None)
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM custom_roles WHERE role_name = ?", (name,))
            return cursor.rowcount > 0

    def list_roles(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for role, perms in BUILTIN_ROLES.items():
            result[role] = [p.value for p in perms]
        for role, perms in self._custom_roles.items():
            result[role] = [p.value for p in perms]
        return result

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
            by_role = conn.execute(
                "SELECT role, COUNT(*) as cnt FROM users WHERE is_active = 1 GROUP BY role"
            ).fetchall()
            total_keys = conn.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 1").fetchone()[0]

        return {
            "total_users": total_users,
            "active_users": active_users,
            "users_by_role": {r["role"]: r["cnt"] for r in by_role},
            "active_api_keys": total_keys,
            "roles": list(BUILTIN_ROLES.keys()) + list(self._custom_roles.keys()),
        }
