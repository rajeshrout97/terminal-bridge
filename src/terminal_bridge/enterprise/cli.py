"""Enterprise CLI commands for Terminal Bridge.

Adds the `tbridge admin` command group with subcommands for fleet management,
user administration, audit queries, policy management, and the dashboard.
"""

from __future__ import annotations

import asyncio
import json

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group("admin")
def admin() -> None:
    """Enterprise administration commands."""


# ═══════════════════════════════════════════════════════
# Dashboard
# ═══════════════════════════════════════════════════════

@admin.command()
@click.option("--host", default="127.0.0.1", help="Dashboard host.")
@click.option("--port", "-p", default=9875, help="Dashboard port.")
def dashboard(host: str, port: int) -> None:
    """Start the admin dashboard and API server."""
    from terminal_bridge.enterprise.dashboard import run_dashboard

    asyncio.run(run_dashboard(host=host, port=port))


# ═══════════════════════════════════════════════════════
# Init
# ═══════════════════════════════════════════════════════

@admin.command()
def init() -> None:
    """Initialize enterprise features and create the first admin user."""
    from terminal_bridge.enterprise.audit import AuditLog
    from terminal_bridge.enterprise.fleet import FleetManager
    from terminal_bridge.enterprise.policies import PolicyEngine
    from terminal_bridge.enterprise.rbac import RBACManager

    console.print("\n[bold]Initializing Terminal Bridge Enterprise[/bold]\n")

    rbac = RBACManager()
    fleet = FleetManager()
    audit = AuditLog()
    policies = PolicyEngine()

    existing = rbac.list_users()
    if existing:
        console.print(f"[yellow]Already initialized with {len(existing)} user(s).[/yellow]")
        return

    name = click.prompt("Admin name")
    email = click.prompt("Admin email")
    user, api_key = rbac.create_user(name=name, email=email, role="admin")

    policy_ids = policies.install_defaults()

    console.print(f"\n[green]Enterprise initialized![/green]\n")
    console.print(f"  Admin user:  {user.name} ({user.email})")
    console.print(f"  Role:        admin")
    console.print(f"  Policies:    {len(policy_ids)} default policies installed")
    console.print(f"\n  [bold]API Key (save this — shown only once):[/bold]")
    console.print(f"  [green]{api_key}[/green]\n")
    console.print(f"  Start the dashboard: [cyan]tbridge admin dashboard[/cyan]")
    console.print(f"  Open in browser:     [link]http://127.0.0.1:9875[/link]\n")


# ═══════════════════════════════════════════════════════
# Fleet
# ═══════════════════════════════════════════════════════

@admin.group()
def fleet() -> None:
    """Fleet management commands."""


@fleet.command("list")
@click.option("--group", "-g", default=None, help="Filter by group.")
@click.option("--status", "-s", default=None, help="Filter by status.")
def fleet_list(group: str | None, status: str | None) -> None:
    """List all registered machines."""
    from terminal_bridge.enterprise.fleet import FleetManager, MachineStatus

    fm = FleetManager()
    machines = fm.list_machines(
        group=group,
        status=MachineStatus(status) if status else None,
    )

    if not machines:
        console.print("[yellow]No machines registered.[/yellow]")
        return

    table = Table(title="Fleet Machines")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Host", style="cyan")
    table.add_column("Group")
    table.add_column("Status")
    table.add_column("Latency")
    table.add_column("Last Seen", style="dim")

    for m in machines:
        status_style = {"online": "green", "offline": "red", "degraded": "yellow"}.get(m.status.value, "dim")
        latency = f"{m.latency_ms:.1f}ms" if m.latency_ms >= 0 else "—"
        table.add_row(
            m.machine_id, m.display_name, f"{m.host}:{m.port}",
            m.group, f"[{status_style}]{m.status.value}[/{status_style}]",
            latency, m.last_seen[:19] if m.last_seen else "—",
        )
    console.print(table)


@fleet.command("add")
@click.argument("hostname")
@click.argument("host")
@click.option("--port", "-p", default=9877, help="Agent port.")
@click.option("--group", "-g", default="default", help="Machine group.")
@click.option("--name", "-n", default="", help="Display name.")
def fleet_add(hostname: str, host: str, port: int, group: str, name: str) -> None:
    """Register a machine in the fleet."""
    from terminal_bridge.enterprise.fleet import FleetManager

    fm = FleetManager()
    machine = fm.register(hostname, host, port=port, display_name=name, group=group)
    console.print(f"[green]Registered:[/green] {machine.display_name} ({machine.machine_id})")


@fleet.command("remove")
@click.argument("machine_id")
def fleet_remove(machine_id: str) -> None:
    """Remove a machine from the fleet."""
    from terminal_bridge.enterprise.fleet import FleetManager

    fm = FleetManager()
    if fm.unregister(machine_id):
        console.print(f"[green]Removed machine {machine_id}[/green]")
    else:
        console.print(f"[red]Machine {machine_id} not found[/red]")


@fleet.command("overview")
def fleet_overview() -> None:
    """Show fleet overview statistics."""
    from terminal_bridge.enterprise.fleet import FleetManager

    fm = FleetManager()
    ov = fm.overview()
    console.print(f"\n[bold]Fleet Overview[/bold]")
    console.print(f"  Total machines: {ov['total_machines']}")
    for status, count in ov.get("by_status", {}).items():
        console.print(f"  {status}: {count}")
    console.print(f"  Potentially stale: {ov['potentially_stale']}\n")


@fleet.command("groups")
def fleet_groups() -> None:
    """List machine groups."""
    from terminal_bridge.enterprise.fleet import FleetManager

    fm = FleetManager()
    groups = fm.list_groups()
    table = Table(title="Groups")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Machines", justify="right")
    for g in groups:
        table.add_row(g["name"], g["description"], str(g["machine_count"]))
    console.print(table)


# ═══════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════

@admin.group()
def users() -> None:
    """User management commands."""


@users.command("list")
def users_list() -> None:
    """List all users."""
    from terminal_bridge.enterprise.rbac import RBACManager

    rbac = RBACManager()
    user_list = rbac.list_users(include_inactive=True)
    if not user_list:
        console.print("[yellow]No users.[/yellow]")
        return

    table = Table(title="Users")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Email")
    table.add_column("Role")
    table.add_column("Active")
    for u in user_list:
        role_style = {"admin": "magenta", "operator": "cyan", "viewer": "dim"}.get(u.role, "")
        table.add_row(
            u.user_id, u.name, u.email,
            f"[{role_style}]{u.role}[/{role_style}]",
            "[green]Yes[/green]" if u.is_active else "[red]No[/red]",
        )
    console.print(table)


@users.command("add")
@click.argument("name")
@click.argument("email")
@click.option("--role", "-r", default="viewer", help="Role: admin, operator, viewer.")
def users_add(name: str, email: str, role: str) -> None:
    """Create a new user and generate an API key."""
    from terminal_bridge.enterprise.rbac import RBACManager

    rbac = RBACManager()
    user, api_key = rbac.create_user(name=name, email=email, role=role)
    console.print(f"\n[green]User created:[/green] {user.name} ({user.email})")
    console.print(f"  Role: {user.role}")
    console.print(f"\n  [bold]API Key (save this — shown only once):[/bold]")
    console.print(f"  [green]{api_key}[/green]\n")


@users.command("deactivate")
@click.argument("user_id")
def users_deactivate(user_id: str) -> None:
    """Deactivate a user."""
    from terminal_bridge.enterprise.rbac import RBACManager

    rbac = RBACManager()
    if rbac.deactivate_user(user_id):
        console.print(f"[green]User {user_id} deactivated[/green]")
    else:
        console.print(f"[red]User {user_id} not found[/red]")


@users.command("roles")
def users_roles() -> None:
    """List all roles and their permissions."""
    from terminal_bridge.enterprise.rbac import RBACManager

    rbac = RBACManager()
    roles = rbac.list_roles()
    for name, perms in roles.items():
        console.print(f"\n[bold]{name}[/bold]")
        for p in perms:
            console.print(f"  • {p}")


# ═══════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════

@admin.group()
def audit() -> None:
    """Audit log commands."""


@audit.command("list")
@click.option("--action", "-a", default=None, help="Filter by action.")
@click.option("--actor", default=None, help="Filter by actor.")
@click.option("--limit", "-n", default=20, help="Number of entries.")
@click.option("--severity", "-s", default=None, help="Filter by severity.")
def audit_list(action: str | None, actor: str | None, limit: int, severity: str | None) -> None:
    """Show recent audit entries."""
    from terminal_bridge.enterprise.audit import AuditAction, AuditLog, AuditSeverity

    al = AuditLog()
    entries = al.query(
        action=AuditAction(action) if action else None,
        actor=actor,
        severity=AuditSeverity(severity) if severity else None,
        limit=limit,
    )

    if not entries:
        console.print("[yellow]No audit entries found.[/yellow]")
        return

    table = Table(title=f"Audit Log (last {limit})")
    table.add_column("Time", style="dim", width=19)
    table.add_column("Severity", width=8)
    table.add_column("Action")
    table.add_column("Actor")
    table.add_column("Remote")
    table.add_column("Detail", max_width=40)

    for e in entries:
        sev_style = {"info": "blue", "warning": "yellow", "critical": "red"}.get(e["severity"], "dim")
        detail = e.get("detail", "{}")
        if isinstance(detail, str):
            detail = detail[:40]
        else:
            detail = json.dumps(detail)[:40]
        table.add_row(
            e["timestamp"][:19], f"[{sev_style}]{e['severity']}[/{sev_style}]",
            e["action"], e["actor"], e["remote"], detail,
        )
    console.print(table)


@audit.command("stats")
def audit_stats() -> None:
    """Show audit log statistics."""
    from terminal_bridge.enterprise.audit import AuditLog

    al = AuditLog()
    stats = al.stats()
    console.print(f"\n[bold]Audit Stats[/bold]")
    console.print(f"  Total entries: {stats['total_entries']}")
    console.print(f"  Critical (24h): {stats['critical_last_24h']}")
    console.print(f"\n  [bold]By action:[/bold]")
    for action, count in stats.get("actions", {}).items():
        console.print(f"    {action}: {count}")


@audit.command("verify")
def audit_verify() -> None:
    """Verify the audit log hash chain integrity."""
    from terminal_bridge.enterprise.audit import AuditLog

    al = AuditLog()
    valid, checked = al.verify_chain()
    if valid:
        console.print(f"[green]Chain VALID[/green] — {checked} entries verified")
    else:
        console.print(f"[red]Chain BROKEN[/red] at entry {checked}")


@audit.command("export")
@click.option("--output", "-o", default="audit-export.json", help="Output file path.")
def audit_export(output: str) -> None:
    """Export audit log to JSON."""
    from terminal_bridge.enterprise.audit import AuditLog

    al = AuditLog()
    data = al.export_json()
    with open(output, "w") as f:
        f.write(data)
    console.print(f"[green]Exported to {output}[/green]")


# ═══════════════════════════════════════════════════════
# Policies
# ═══════════════════════════════════════════════════════

@admin.group()
def policies() -> None:
    """Policy management commands."""


@policies.command("list")
def policies_list() -> None:
    """List all policies."""
    from terminal_bridge.enterprise.policies import PolicyEngine

    pe = PolicyEngine()
    pols = pe.list_policies(active_only=False)
    if not pols:
        console.print("[yellow]No policies configured.[/yellow]")
        return

    table = Table(title="Policies")
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Action")
    table.add_column("Priority", justify="right")
    table.add_column("Scope")
    table.add_column("Active")

    for p in pols:
        action_style = {"block": "red", "allow": "green", "require_approval": "yellow", "log_only": "blue"}.get(p.action.value, "")
        table.add_row(
            p.policy_id, p.name,
            f"[{action_style}]{p.action.value}[/{action_style}]",
            str(p.priority),
            f"{p.scope.value}{': ' + p.scope_value if p.scope_value else ''}",
            "[green]Yes[/green]" if p.is_active else "[red]No[/red]",
        )
    console.print(table)


@policies.command("install-defaults")
def policies_install_defaults() -> None:
    """Install default security policies."""
    from terminal_bridge.enterprise.policies import PolicyEngine

    pe = PolicyEngine()
    ids = pe.install_defaults()
    console.print(f"[green]Installed {len(ids)} default policies[/green]")


@policies.command("test")
@click.argument("command")
def policies_test(command: str) -> None:
    """Test a command against the current policies."""
    from terminal_bridge.enterprise.policies import PolicyEngine

    pe = PolicyEngine()
    result = pe.evaluate(command)
    if result.allowed:
        console.print(f"[green]ALLOWED[/green] — {result.reason}")
    else:
        console.print(f"[red]BLOCKED[/red] — {result.reason}")
        if result.requires_approval:
            console.print("[yellow]This command requires approval.[/yellow]")


@policies.command("approvals")
def policies_approvals() -> None:
    """List pending approval requests."""
    from terminal_bridge.enterprise.policies import PolicyEngine

    pe = PolicyEngine()
    pending = pe.get_pending_approvals()
    if not pending:
        console.print("[green]No pending approvals.[/green]")
        return

    table = Table(title="Pending Approvals")
    table.add_column("ID", style="dim")
    table.add_column("Actor")
    table.add_column("Remote")
    table.add_column("Command")
    table.add_column("Requested", style="dim")

    for a in pending:
        table.add_row(a["request_id"], a["actor"], a["remote"], a["command"], a["created_at"][:19])
    console.print(table)
