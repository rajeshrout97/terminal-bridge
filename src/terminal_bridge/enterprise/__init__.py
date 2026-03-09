"""Terminal Bridge Enterprise — Fleet management, RBAC, audit logging, and policy enforcement."""

from terminal_bridge.version import __version__

__all__ = [
    "AuditLog",
    "RBACManager",
    "FleetManager",
    "PolicyEngine",
]


def __getattr__(name: str):
    _imports = {
        "AuditLog": "terminal_bridge.enterprise.audit",
        "RBACManager": "terminal_bridge.enterprise.rbac",
        "FleetManager": "terminal_bridge.enterprise.fleet",
        "PolicyEngine": "terminal_bridge.enterprise.policies",
    }
    if name in _imports:
        import importlib
        mod = importlib.import_module(_imports[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
