"""Enterprise admin REST API.

Exposes fleet management, RBAC, audit logs, and policy engine over HTTP.
All endpoints require API key authentication via Bearer token.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aiohttp import web

from terminal_bridge.enterprise.audit import AuditAction, AuditLog, AuditSeverity
from terminal_bridge.enterprise.fleet import FleetManager, MachineStatus
from terminal_bridge.enterprise.policies import (
    Policy,
    PolicyAction,
    PolicyEngine,
    PolicyScope,
)
from terminal_bridge.enterprise.rbac import Permission, RBACManager, User


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def require_auth(*permissions: Permission):
    """Decorator that authenticates via Bearer token and checks permissions."""
    def decorator(handler):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            rbac: RBACManager = request.app["rbac"]
            audit: AuditLog = request.app["audit"]

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return _error("Missing or invalid Authorization header", 401)

            api_key = auth_header[7:]
            user = rbac.authenticate(api_key)
            if user is None:
                audit.log(AuditAction.AUTH_FAILURE, actor="unknown", remote="admin-api",
                          detail={"reason": "invalid_api_key"}, severity=AuditSeverity.WARNING)
                return _error("Invalid API key", 401)

            for perm in permissions:
                if not rbac.has_permission(user, perm):
                    audit.log(AuditAction.RBAC_DENY, actor=user.email, remote="admin-api",
                              detail={"permission": perm.value}, severity=AuditSeverity.WARNING)
                    return _error(f"Insufficient permission: {perm.value}", 403)

            request["user"] = user
            return await handler(request)
        return wrapper
    return decorator


def build_admin_app(
    rbac: RBACManager,
    fleet: FleetManager,
    audit: AuditLog,
    policies: PolicyEngine,
) -> web.Application:
    app = web.Application()
    app["rbac"] = rbac
    app["fleet"] = fleet
    app["audit"] = audit
    app["policies"] = policies

    # ── Fleet ──
    app.router.add_get("/admin/fleet/overview", fleet_overview)
    app.router.add_get("/admin/fleet/machines", fleet_list)
    app.router.add_post("/admin/fleet/machines", fleet_register)
    app.router.add_get("/admin/fleet/machines/{id}", fleet_get)
    app.router.add_patch("/admin/fleet/machines/{id}", fleet_update)
    app.router.add_delete("/admin/fleet/machines/{id}", fleet_remove)
    app.router.add_get("/admin/fleet/machines/{id}/health", fleet_health)
    app.router.add_post("/admin/fleet/batch-exec", fleet_batch_exec)
    app.router.add_get("/admin/fleet/groups", fleet_groups)
    app.router.add_post("/admin/fleet/groups", fleet_create_group)

    # ── Users ──
    app.router.add_get("/admin/users", users_list)
    app.router.add_post("/admin/users", users_create)
    app.router.add_get("/admin/users/{id}", users_get)
    app.router.add_patch("/admin/users/{id}", users_update)
    app.router.add_delete("/admin/users/{id}", users_deactivate)
    app.router.add_post("/admin/users/{id}/api-keys", users_create_key)
    app.router.add_get("/admin/users/{id}/api-keys", users_list_keys)

    # ── Roles ──
    app.router.add_get("/admin/roles", roles_list)
    app.router.add_post("/admin/roles", roles_create)

    # ── Audit ──
    app.router.add_get("/admin/audit", audit_query)
    app.router.add_get("/admin/audit/stats", audit_stats)
    app.router.add_get("/admin/audit/export", audit_export)
    app.router.add_post("/admin/audit/verify", audit_verify)

    # ── Policies ──
    app.router.add_get("/admin/policies", policies_list)
    app.router.add_post("/admin/policies", policies_create)
    app.router.add_get("/admin/policies/{id}", policies_get)
    app.router.add_patch("/admin/policies/{id}", policies_update)
    app.router.add_delete("/admin/policies/{id}", policies_delete)
    app.router.add_post("/admin/policies/evaluate", policies_evaluate)
    app.router.add_post("/admin/policies/install-defaults", policies_install_defaults)
    app.router.add_get("/admin/policies/approvals", policies_pending_approvals)
    app.router.add_post("/admin/policies/approvals/{id}/review", policies_review_approval)

    # ── Dashboard data ──
    app.router.add_get("/admin/dashboard", dashboard_data)

    return app


# ═══════════════════════════════════════════════════════
# Fleet endpoints
# ═══════════════════════════════════════════════════════

@require_auth(Permission.FLEET_VIEW)
async def fleet_overview(request: web.Request) -> web.Response:
    return _json(request.app["fleet"].overview())


@require_auth(Permission.FLEET_VIEW)
async def fleet_list(request: web.Request) -> web.Response:
    fleet: FleetManager = request.app["fleet"]
    group = request.query.get("group")
    status = request.query.get("status")
    machines = fleet.list_machines(
        group=group,
        status=MachineStatus(status) if status else None,
    )
    return _json([{
        "machine_id": m.machine_id, "hostname": m.hostname, "host": m.host,
        "port": m.port, "display_name": m.display_name, "group": m.group,
        "tags": m.tags, "status": m.status.value, "latency_ms": m.latency_ms,
        "last_seen": m.last_seen, "agent_version": m.agent_version,
    } for m in machines])


@require_auth(Permission.FLEET_MANAGE)
async def fleet_register(request: web.Request) -> web.Response:
    body = await request.json()
    fleet: FleetManager = request.app["fleet"]
    machine = fleet.register(
        hostname=body["hostname"], host=body["host"],
        port=body.get("port", 9877), display_name=body.get("display_name", ""),
        group=body.get("group", "default"), tags=body.get("tags", []),
    )
    request.app["audit"].log(
        AuditAction.ADMIN_ACTION, actor=request["user"].email, remote=machine.hostname,
        detail={"action": "fleet.register", "machine_id": machine.machine_id},
    )
    return _json({"machine_id": machine.machine_id, "hostname": machine.hostname}, 201)


@require_auth(Permission.FLEET_VIEW)
async def fleet_get(request: web.Request) -> web.Response:
    machine = request.app["fleet"].get(request.match_info["id"])
    if not machine:
        return _error("Machine not found", 404)
    return _json({
        "machine_id": machine.machine_id, "hostname": machine.hostname,
        "host": machine.host, "port": machine.port,
        "display_name": machine.display_name, "group": machine.group,
        "tags": machine.tags, "status": machine.status.value,
        "os_version": machine.os_version, "cpu_arch": machine.cpu_arch,
        "latency_ms": machine.latency_ms, "last_seen": machine.last_seen,
        "agent_version": machine.agent_version, "metadata": machine.metadata,
        "registered_at": machine.registered_at,
    })


@require_auth(Permission.FLEET_MANAGE)
async def fleet_update(request: web.Request) -> web.Response:
    body = await request.json()
    ok = request.app["fleet"].update(request.match_info["id"], **body)
    if not ok:
        return _error("Machine not found or no changes", 404)
    return _json({"ok": True})


@require_auth(Permission.FLEET_MANAGE)
async def fleet_remove(request: web.Request) -> web.Response:
    ok = request.app["fleet"].unregister(request.match_info["id"])
    if not ok:
        return _error("Machine not found", 404)
    request.app["audit"].log(
        AuditAction.ADMIN_ACTION, actor=request["user"].email, remote="",
        detail={"action": "fleet.unregister", "machine_id": request.match_info["id"]},
    )
    return _json({"ok": True})


@require_auth(Permission.FLEET_VIEW)
async def fleet_health(request: web.Request) -> web.Response:
    hours = int(request.query.get("hours", "24"))
    history = request.app["fleet"].get_health_history(request.match_info["id"], hours=hours)
    return _json(history)


@require_auth(Permission.FLEET_MANAGE)
async def fleet_batch_exec(request: web.Request) -> web.Response:
    body = await request.json()
    fleet: FleetManager = request.app["fleet"]
    result = await fleet.batch_exec(
        body["command"], group=body.get("group"), machine_ids=body.get("machine_ids"),
        timeout=body.get("timeout", 30),
    )
    request.app["audit"].log(
        AuditAction.COMMAND_EXEC, actor=request["user"].email, remote="batch",
        detail={"command": body["command"], "machines": result["executed_on"]},
    )
    return _json(result)


@require_auth(Permission.FLEET_VIEW)
async def fleet_groups(request: web.Request) -> web.Response:
    return _json(request.app["fleet"].list_groups())


@require_auth(Permission.FLEET_MANAGE)
async def fleet_create_group(request: web.Request) -> web.Response:
    body = await request.json()
    request.app["fleet"].create_group(body["name"], body.get("description", ""))
    return _json({"ok": True}, 201)


# ═══════════════════════════════════════════════════════
# User endpoints
# ═══════════════════════════════════════════════════════

@require_auth(Permission.USER_VIEW)
async def users_list(request: web.Request) -> web.Response:
    users = request.app["rbac"].list_users()
    return _json([{
        "user_id": u.user_id, "name": u.name, "email": u.email,
        "role": u.role, "is_active": u.is_active, "last_seen": u.last_seen,
    } for u in users])


@require_auth(Permission.USER_MANAGE)
async def users_create(request: web.Request) -> web.Response:
    body = await request.json()
    user, api_key = request.app["rbac"].create_user(
        name=body["name"], email=body["email"], role=body.get("role", "viewer"),
    )
    request.app["audit"].log(
        AuditAction.ADMIN_ACTION, actor=request["user"].email, remote="admin-api",
        detail={"action": "user.create", "new_user": user.email, "role": user.role},
    )
    return _json({"user_id": user.user_id, "email": user.email, "api_key": api_key}, 201)


@require_auth(Permission.USER_VIEW)
async def users_get(request: web.Request) -> web.Response:
    user = request.app["rbac"].get_user(request.match_info["id"])
    if not user:
        return _error("User not found", 404)
    return _json({
        "user_id": user.user_id, "name": user.name, "email": user.email,
        "role": user.role, "is_active": user.is_active,
        "created_at": user.created_at, "last_seen": user.last_seen,
    })


@require_auth(Permission.USER_MANAGE)
async def users_update(request: web.Request) -> web.Response:
    body = await request.json()
    ok = request.app["rbac"].update_user(request.match_info["id"], **body)
    if not ok:
        return _error("User not found or no changes", 404)
    return _json({"ok": True})


@require_auth(Permission.USER_MANAGE)
async def users_deactivate(request: web.Request) -> web.Response:
    ok = request.app["rbac"].deactivate_user(request.match_info["id"])
    if not ok:
        return _error("User not found", 404)
    request.app["audit"].log(
        AuditAction.ADMIN_ACTION, actor=request["user"].email, remote="admin-api",
        detail={"action": "user.deactivate", "target": request.match_info["id"]},
    )
    return _json({"ok": True})


@require_auth(Permission.USER_MANAGE)
async def users_create_key(request: web.Request) -> web.Response:
    body = await request.json()
    api_key = request.app["rbac"].create_api_key(
        request.match_info["id"], label=body.get("label", ""),
        expires_in_days=body.get("expires_in_days"),
    )
    return _json({"api_key": api_key}, 201)


@require_auth(Permission.USER_VIEW)
async def users_list_keys(request: web.Request) -> web.Response:
    keys = request.app["rbac"].list_api_keys(request.match_info["id"])
    return _json(keys)


# ═══════════════════════════════════════════════════════
# Roles
# ═══════════════════════════════════════════════════════

@require_auth(Permission.USER_VIEW)
async def roles_list(request: web.Request) -> web.Response:
    return _json(request.app["rbac"].list_roles())


@require_auth(Permission.USER_MANAGE)
async def roles_create(request: web.Request) -> web.Response:
    body = await request.json()
    from terminal_bridge.enterprise.rbac import Permission as P
    perms = [P(p) for p in body["permissions"]]
    request.app["rbac"].create_role(body["name"], perms, body.get("description", ""))
    return _json({"ok": True}, 201)


# ═══════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════

@require_auth(Permission.AUDIT_VIEW)
async def audit_query(request: web.Request) -> web.Response:
    audit: AuditLog = request.app["audit"]
    kwargs: dict[str, Any] = {}
    if "action" in request.query:
        kwargs["action"] = AuditAction(request.query["action"])
    if "actor" in request.query:
        kwargs["actor"] = request.query["actor"]
    if "remote" in request.query:
        kwargs["remote"] = request.query["remote"]
    if "severity" in request.query:
        kwargs["severity"] = AuditSeverity(request.query["severity"])
    if "since" in request.query:
        kwargs["since"] = datetime.fromisoformat(request.query["since"])
    kwargs["limit"] = int(request.query.get("limit", "100"))
    kwargs["offset"] = int(request.query.get("offset", "0"))
    return _json(audit.query(**kwargs))


@require_auth(Permission.AUDIT_VIEW)
async def audit_stats(request: web.Request) -> web.Response:
    return _json(request.app["audit"].stats())


@require_auth(Permission.AUDIT_EXPORT)
async def audit_export(request: web.Request) -> web.Response:
    since = None
    if "since" in request.query:
        since = datetime.fromisoformat(request.query["since"])
    data = request.app["audit"].export_json(since=since)
    return web.Response(
        text=data, content_type="application/json",
        headers={"Content-Disposition": "attachment; filename=audit-export.json"},
    )


@require_auth(Permission.AUDIT_VIEW)
async def audit_verify(request: web.Request) -> web.Response:
    valid, checked = request.app["audit"].verify_chain()
    return _json({"valid": valid, "entries_checked": checked})


# ═══════════════════════════════════════════════════════
# Policies
# ═══════════════════════════════════════════════════════

@require_auth(Permission.POLICY_VIEW)
async def policies_list(request: web.Request) -> web.Response:
    engine: PolicyEngine = request.app["policies"]
    policies = engine.list_policies(active_only="active_only" in request.query)
    return _json([{
        "policy_id": p.policy_id, "name": p.name, "description": p.description,
        "action": p.action.value, "priority": p.priority, "is_active": p.is_active,
        "scope": p.scope.value, "scope_value": p.scope_value,
    } for p in policies])


@require_auth(Permission.POLICY_MANAGE)
async def policies_create(request: web.Request) -> web.Response:
    body = await request.json()
    policy = Policy(
        policy_id="", name=body["name"], description=body.get("description", ""),
        action=PolicyAction(body.get("action", "block")),
        priority=body.get("priority", 100),
        scope=PolicyScope(body.get("scope", "global")),
        scope_value=body.get("scope_value", ""),
        command_patterns=body.get("command_patterns", []),
        blocked_commands=body.get("blocked_commands", []),
        allowed_commands=body.get("allowed_commands", []),
        blocked_paths=body.get("blocked_paths", []),
        max_timeout=body.get("max_timeout", 0),
    )
    created = request.app["policies"].create_policy(policy)
    request.app["audit"].log(
        AuditAction.ADMIN_ACTION, actor=request["user"].email, remote="admin-api",
        detail={"action": "policy.create", "policy": created.name},
    )
    return _json({"policy_id": created.policy_id}, 201)


@require_auth(Permission.POLICY_VIEW)
async def policies_get(request: web.Request) -> web.Response:
    policy = request.app["policies"].get_policy(request.match_info["id"])
    if not policy:
        return _error("Policy not found", 404)
    return _json({
        "policy_id": policy.policy_id, "name": policy.name,
        "description": policy.description, "action": policy.action.value,
        "priority": policy.priority, "is_active": policy.is_active,
        "scope": policy.scope.value, "scope_value": policy.scope_value,
        "command_patterns": policy.command_patterns,
        "blocked_commands": policy.blocked_commands,
        "allowed_commands": policy.allowed_commands,
        "blocked_paths": policy.blocked_paths,
        "max_timeout": policy.max_timeout,
    })


@require_auth(Permission.POLICY_MANAGE)
async def policies_update(request: web.Request) -> web.Response:
    body = await request.json()
    ok = request.app["policies"].update_policy(request.match_info["id"], **body)
    if not ok:
        return _error("Policy not found or no changes", 404)
    return _json({"ok": True})


@require_auth(Permission.POLICY_MANAGE)
async def policies_delete(request: web.Request) -> web.Response:
    ok = request.app["policies"].delete_policy(request.match_info["id"])
    if not ok:
        return _error("Policy not found", 404)
    return _json({"ok": True})


@require_auth(Permission.POLICY_VIEW)
async def policies_evaluate(request: web.Request) -> web.Response:
    body = await request.json()
    result = request.app["policies"].evaluate(
        body["command"], actor=request["user"].email,
        actor_role=request["user"].role,
        remote=body.get("remote", ""), remote_group=body.get("remote_group", ""),
    )
    return _json({
        "allowed": result.allowed, "action": result.action.value,
        "reason": result.reason, "requires_approval": result.requires_approval,
        "policy_id": result.policy_id, "policy_name": result.policy_name,
    })


@require_auth(Permission.POLICY_MANAGE)
async def policies_install_defaults(request: web.Request) -> web.Response:
    ids = request.app["policies"].install_defaults()
    return _json({"installed": len(ids), "policy_ids": ids})


@require_auth(Permission.POLICY_VIEW)
async def policies_pending_approvals(request: web.Request) -> web.Response:
    return _json(request.app["policies"].get_pending_approvals())


@require_auth(Permission.POLICY_MANAGE)
async def policies_review_approval(request: web.Request) -> web.Response:
    body = await request.json()
    ok = request.app["policies"].review_approval(
        request.match_info["id"], approved=body["approved"],
        reviewer=request["user"].email, note=body.get("note", ""),
    )
    if not ok:
        return _error("Approval not found or already reviewed", 404)
    return _json({"ok": True})


# ═══════════════════════════════════════════════════════
# Dashboard aggregate
# ═══════════════════════════════════════════════════════

@require_auth(Permission.FLEET_VIEW)
async def dashboard_data(request: web.Request) -> web.Response:
    return _json({
        "fleet": request.app["fleet"].overview(),
        "users": request.app["rbac"].stats(),
        "audit": request.app["audit"].stats(),
        "policies": request.app["policies"].stats(),
    })


# ═══════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════

async def run_admin_api(*, host: str = "127.0.0.1", port: int = 9875) -> None:
    from pathlib import Path
    db_path = Path.home() / ".config" / "terminal-bridge" / "enterprise.db"

    rbac = RBACManager(db_path)
    fleet = FleetManager(db_path)
    audit = AuditLog()
    policies = PolicyEngine(db_path)

    app = build_admin_app(rbac, fleet, audit, policies)

    from rich.console import Console
    console = Console()
    console.print(f"[bold green]Enterprise Admin API[/bold green] running on http://{host}:{port}")
    console.print("Endpoints: /admin/fleet, /admin/users, /admin/audit, /admin/policies, /admin/dashboard")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    import asyncio
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
