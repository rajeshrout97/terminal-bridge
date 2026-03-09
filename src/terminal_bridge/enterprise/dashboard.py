"""Enterprise admin dashboard.

Serves a single-page web dashboard for fleet management, audit logs,
user management, and policy administration. Built as an aiohttp app
that serves the HTML directly — no separate frontend build step.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from terminal_bridge.enterprise.admin_api import build_admin_app, run_admin_api
from terminal_bridge.enterprise.audit import AuditLog
from terminal_bridge.enterprise.fleet import FleetManager
from terminal_bridge.enterprise.policies import PolicyEngine
from terminal_bridge.enterprise.rbac import RBACManager

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terminal Bridge — Admin Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--s1:#12121a;--s2:#1a1a26;--bd:#252535;--tx:#e0e0ee;--tm:#7a7a90;--ac:#6366f1;--gn:#22c55e;--rd:#ef4444;--yl:#f59e0b;--cy:#06b6d4}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--tx);-webkit-font-smoothing:antialiased}
.shell{display:grid;grid-template-columns:240px 1fr;height:100vh}
.sidebar{background:var(--s1);border-right:1px solid var(--bd);padding:20px 0;display:flex;flex-direction:column}
.sidebar h1{font-size:15px;font-weight:700;padding:0 20px 24px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px}
.sidebar h1 span{background:linear-gradient(135deg,var(--ac),var(--cy));border-radius:6px;padding:3px 7px;font-size:11px;color:#fff}
.nav{flex:1;padding:16px 0}
.nav a{display:flex;align-items:center;gap:10px;padding:10px 20px;color:var(--tm);text-decoration:none;font-size:13px;font-weight:500;border-left:3px solid transparent;transition:all .2s}
.nav a:hover,.nav a.active{color:var(--tx);background:var(--s2);border-left-color:var(--ac)}
.nav a svg{width:16px;height:16px;opacity:.5}
.main{overflow-y:auto;padding:32px}
.main h2{font-size:22px;font-weight:700;margin-bottom:8px}
.main .sub{color:var(--tm);font-size:14px;margin-bottom:28px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:32px}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:20px}
.card .label{font-size:12px;color:var(--tm);font-weight:500;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.card .value{font-size:28px;font-weight:700}
.card .value.green{color:var(--gn)}.card .value.red{color:var(--rd)}.card .value.yellow{color:var(--yl)}.card .value.blue{color:var(--ac)}
.table-wrap{background:var(--s1);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:12px 16px;font-size:11px;font-weight:600;color:var(--tm);text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--bd);background:var(--s2)}
td{padding:12px 16px;font-size:13px;border-bottom:1px solid var(--bd)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(99,102,241,.04)}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600}
.badge.online{background:rgba(34,197,94,.15);color:var(--gn)}
.badge.offline{background:rgba(239,68,68,.15);color:var(--rd)}
.badge.degraded{background:rgba(245,158,11,.15);color:var(--yl)}
.badge.admin{background:rgba(99,102,241,.15);color:var(--ac)}
.badge.operator{background:rgba(6,182,212,.15);color:var(--cy)}
.badge.viewer{background:rgba(122,122,144,.15);color:var(--tm)}
.badge.block{background:rgba(239,68,68,.15);color:var(--rd)}
.badge.allow{background:rgba(34,197,94,.15);color:var(--gn)}
.badge.require_approval{background:rgba(245,158,11,.15);color:var(--yl)}
.badge.log_only{background:rgba(99,102,241,.15);color:var(--ac)}
.badge.info{background:rgba(99,102,241,.1);color:var(--ac)}
.badge.warning{background:rgba(245,158,11,.15);color:var(--yl)}
.badge.critical{background:rgba(239,68,68,.15);color:var(--rd)}
.mono{font-family:'JetBrains Mono',monospace;font-size:12px}
.toolbar{display:flex;gap:12px;margin-bottom:20px;align-items:center}
input[type=text],select{background:var(--s2);border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:8px 14px;font-size:13px;font-family:'Inter',sans-serif}
input[type=text]:focus,select:focus{outline:none;border-color:var(--ac)}
button{background:var(--ac);color:#fff;border:none;border-radius:8px;padding:8px 18px;font-size:13px;font-weight:600;cursor:pointer;transition:background .2s}
button:hover{background:#818cf8}
button.secondary{background:var(--s2);border:1px solid var(--bd);color:var(--tx)}
.empty{text-align:center;padding:48px;color:var(--tm);font-size:14px}
#content{min-height:400px}
.api-key-display{background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:12px 16px;font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--gn);margin:12px 0}
.hidden{display:none}
@media(max-width:768px){.shell{grid-template-columns:1fr}.sidebar{display:none}}
</style>
</head>
<body>
<div class="shell">
<aside class="sidebar">
    <h1><span>TB</span> Admin</h1>
    <nav class="nav" id="nav">
        <a href="#" data-page="dashboard" class="active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> Dashboard</a>
        <a href="#" data-page="fleet"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg> Fleet</a>
        <a href="#" data-page="users"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg> Users</a>
        <a href="#" data-page="policies"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> Policies</a>
        <a href="#" data-page="audit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/></svg> Audit Log</a>
    </nav>
</aside>
<main class="main"><div id="content"></div></main>
</div>

<script>
const API = '';
let KEY = localStorage.getItem('tb_admin_key') || '';

async function api(path, opts = {}) {
    const res = await fetch(API + path, {
        ...opts,
        headers: { 'Authorization': `Bearer ${KEY}`, 'Content-Type': 'application/json', ...opts.headers },
        body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) { KEY = prompt('Enter your admin API key:') || ''; localStorage.setItem('tb_admin_key', KEY); return api(path, opts); }
    return res.json();
}

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

// ── Pages ──
const pages = {
    async dashboard() {
        const d = await api('/admin/dashboard');
        const f = d.fleet || {}; const u = d.users || {}; const a = d.audit || {}; const p = d.policies || {};
        return `
        <h2>Dashboard</h2><p class="sub">Fleet overview and system health</p>
        <div class="cards">
            <div class="card"><div class="label">Total Machines</div><div class="value blue">${f.total_machines||0}</div></div>
            <div class="card"><div class="label">Online</div><div class="value green">${(f.by_status||{}).online||0}</div></div>
            <div class="card"><div class="label">Offline</div><div class="value red">${(f.by_status||{}).offline||0}</div></div>
            <div class="card"><div class="label">Active Users</div><div class="value blue">${u.active_users||0}</div></div>
            <div class="card"><div class="label">Audit Entries</div><div class="value">${a.total_entries||0}</div></div>
            <div class="card"><div class="label">Critical (24h)</div><div class="value ${(a.critical_last_24h||0)>0?'red':'green'}">${a.critical_last_24h||0}</div></div>
            <div class="card"><div class="label">Active Policies</div><div class="value">${p.active_policies||0}</div></div>
            <div class="card"><div class="label">Pending Approvals</div><div class="value ${(p.pending_approvals||0)>0?'yellow':''}">${p.pending_approvals||0}</div></div>
        </div>`;
    },

    async fleet() {
        const machines = await api('/admin/fleet/machines');
        let rows = machines.map(m => `<tr>
            <td class="mono">${m.machine_id}</td>
            <td><strong>${m.display_name}</strong><br><span style="color:var(--tm);font-size:12px">${m.hostname}</span></td>
            <td class="mono">${m.host}:${m.port}</td>
            <td>${m.group}</td>
            <td><span class="badge ${m.status}">${m.status}</span></td>
            <td>${m.latency_ms >= 0 ? m.latency_ms.toFixed(1) + 'ms' : '—'}</td>
            <td style="color:var(--tm);font-size:12px">${m.last_seen ? new Date(m.last_seen).toLocaleString() : '—'}</td>
        </tr>`).join('');
        if (!rows) rows = '<tr><td colspan="7" class="empty">No machines registered</td></tr>';
        return `
        <h2>Fleet</h2><p class="sub">Manage your remote Macs</p>
        <div class="table-wrap"><table>
            <thead><tr><th>ID</th><th>Name</th><th>Address</th><th>Group</th><th>Status</th><th>Latency</th><th>Last Seen</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;
    },

    async users() {
        const users = await api('/admin/users');
        let rows = users.map(u => `<tr>
            <td class="mono">${u.user_id}</td>
            <td><strong>${u.name}</strong></td>
            <td>${u.email}</td>
            <td><span class="badge ${u.role}">${u.role}</span></td>
            <td><span class="badge ${u.is_active?'online':'offline'}">${u.is_active?'Active':'Inactive'}</span></td>
            <td style="color:var(--tm);font-size:12px">${u.last_seen ? new Date(u.last_seen).toLocaleString() : '—'}</td>
        </tr>`).join('');
        if (!rows) rows = '<tr><td colspan="6" class="empty">No users yet</td></tr>';
        return `
        <h2>Users</h2><p class="sub">Manage users, roles, and API keys</p>
        <div class="toolbar">
            <button onclick="createUserPrompt()">+ Add User</button>
        </div>
        <div class="table-wrap"><table>
            <thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last Seen</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;
    },

    async policies() {
        const pols = await api('/admin/policies');
        let rows = pols.map(p => `<tr>
            <td class="mono">${p.policy_id}</td>
            <td><strong>${p.name}</strong><br><span style="color:var(--tm);font-size:12px">${p.description||''}</span></td>
            <td><span class="badge ${p.action}">${p.action}</span></td>
            <td>${p.priority}</td>
            <td>${p.scope}${p.scope_value ? ': '+p.scope_value : ''}</td>
            <td><span class="badge ${p.is_active?'online':'offline'}">${p.is_active?'Active':'Off'}</span></td>
        </tr>`).join('');
        if (!rows) rows = '<tr><td colspan="6" class="empty">No policies configured</td></tr>';
        return `
        <h2>Policies</h2><p class="sub">Command restrictions and approval workflows</p>
        <div class="toolbar">
            <button onclick="installDefaults()">Install Default Policies</button>
        </div>
        <div class="table-wrap"><table>
            <thead><tr><th>ID</th><th>Name</th><th>Action</th><th>Priority</th><th>Scope</th><th>Status</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;
    },

    async audit() {
        const entries = await api('/admin/audit?limit=50');
        let rows = entries.map(e => {
            const d = typeof e.detail === 'string' ? JSON.parse(e.detail) : e.detail;
            return `<tr>
                <td style="color:var(--tm);font-size:12px;white-space:nowrap">${new Date(e.timestamp).toLocaleString()}</td>
                <td><span class="badge ${e.severity}">${e.severity}</span></td>
                <td class="mono">${e.action}</td>
                <td>${e.actor}</td>
                <td>${e.remote}</td>
                <td class="mono" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${JSON.stringify(d).slice(0,80)}</td>
            </tr>`;
        }).join('');
        if (!rows) rows = '<tr><td colspan="6" class="empty">No audit entries yet</td></tr>';
        return `
        <h2>Audit Log</h2><p class="sub">Tamper-evident record of all operations</p>
        <div class="toolbar">
            <button class="secondary" onclick="verifyChain()">Verify Chain Integrity</button>
            <button class="secondary" onclick="location.href=API+'/admin/audit/export'">Export JSON</button>
        </div>
        <div class="table-wrap"><table>
            <thead><tr><th>Time</th><th>Severity</th><th>Action</th><th>Actor</th><th>Remote</th><th>Detail</th></tr></thead>
            <tbody>${rows}</tbody>
        </table></div>`;
    },
};

async function navigate(page) {
    $$('.nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
    try {
        $('#content').innerHTML = await pages[page]();
    } catch (e) {
        $('#content').innerHTML = `<div class="empty">Error loading ${page}: ${e.message}</div>`;
    }
}

$('#nav').addEventListener('click', e => {
    const a = e.target.closest('a[data-page]');
    if (a) { e.preventDefault(); navigate(a.dataset.page); }
});

async function createUserPrompt() {
    const name = prompt('Name:'); if (!name) return;
    const email = prompt('Email:'); if (!email) return;
    const role = prompt('Role (admin/operator/viewer):', 'viewer');
    const res = await api('/admin/users', { method: 'POST', body: { name, email, role } });
    if (res.api_key) {
        alert('User created!\\n\\nAPI Key (save this — shown only once):\\n' + res.api_key);
        navigate('users');
    } else {
        alert('Error: ' + JSON.stringify(res));
    }
}

async function installDefaults() {
    const res = await api('/admin/policies/install-defaults', { method: 'POST' });
    alert(`Installed ${res.installed} default policies`);
    navigate('policies');
}

async function verifyChain() {
    const res = await api('/admin/audit/verify', { method: 'POST' });
    alert(res.valid ? `Chain VALID — ${res.entries_checked} entries verified` : `Chain BROKEN at entry ${res.entries_checked}`);
}

if (!KEY) { KEY = prompt('Enter your admin API key:') || ''; localStorage.setItem('tb_admin_key', KEY); }
navigate('dashboard');
</script>
</body>
</html>"""


async def serve_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def run_dashboard(*, host: str = "127.0.0.1", port: int = 9875) -> None:
    """Start the admin API with the dashboard UI on the same port."""
    from pathlib import Path
    db_path = Path.home() / ".config" / "terminal-bridge" / "enterprise.db"

    rbac = RBACManager(db_path)
    fleet = FleetManager(db_path)
    audit = AuditLog()
    policy_engine = PolicyEngine(db_path)

    app = build_admin_app(rbac, fleet, audit, policy_engine)
    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/dashboard", serve_dashboard)

    from rich.console import Console
    console = Console()
    console.print(f"\n[bold]Terminal Bridge Enterprise Admin[/bold]")
    console.print(f"  Dashboard:  [link]http://{host}:{port}[/link]")
    console.print(f"  API:        [link]http://{host}:{port}/admin[/link]")
    console.print()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
