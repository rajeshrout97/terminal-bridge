"""Terminal Bridge -- Model-agnostic remote Mac terminal access for AI agents."""

from terminal_bridge.version import __version__

__all__ = ["__version__", "RemoteTerminal"]


def __getattr__(name: str):
    """Lazy import for the public SDK API."""
    if name == "RemoteTerminal":
        from terminal_bridge.local_bridge.client import RemoteTerminal

        return RemoteTerminal
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

