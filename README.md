# Terminal Bridge

**Model-agnostic remote Mac terminal access for AI agents.**

Control one Mac's terminal from another using any AI tool — Cursor, Claude CLI, Ollama, or custom scripts. The AI runs on your local machine; commands execute on the remote.

[![PyPI version](https://img.shields.io/pypi/v/terminal-bridge.svg)](https://pypi.org/project/terminal-bridge/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/terminal-bridge/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/rajeshrout97/terminal-bridge/blob/main/LICENSE)

```
┌─────────────────────────┐              ┌─────────────────────────┐
│   LOCAL MAC              │              │   REMOTE MAC            │
│                         │              │                         │
│  Cursor / Ollama / Any  │───Network───▶│  Terminal Bridge Agent  │
│  AI runs HERE using     │   (WiFi /    │  Executes commands      │
│  YOUR local compute     │   Internet)  │  on THIS machine        │
└─────────────────────────┘              └─────────────────────────┘
```

---

## Why Terminal Bridge?

- **Model-agnostic** — works with Cursor, Claude CLI, Ollama, LM Studio, aider, or any tool that speaks HTTP/MCP/stdio
- **Six interfaces** — CLI, REST API, Python SDK, MCP (Cursor), stdio pipe, interactive terminal
- **Secure by default** — HMAC-SHA256 auth, TLS encryption, macOS Keychain for key storage
- **Zero config for Cursor** — auto-configures MCP so the AI agent "just works"
- **Background service** — launchd agent auto-starts on login, survives reboots
- **LAN + Internet** — works over WiFi, Tailscale, or the built-in relay server

---

## Quick Start

### Install

```bash
pip install terminal-bridge
# or
pipx install terminal-bridge
```

### On the remote Mac (the one you want to control)

```bash
tbridge setup remote
```

This generates a pairing code. Copy it.

### On the local Mac (where your AI runs)

```bash
tbridge setup local <PAIRING_CODE>
```

Done. Cursor's MCP is auto-configured. Restart Cursor and start talking to your remote Mac.

---

## Interfaces

| Interface | Best For | Example |
|-----------|----------|---------|
| **CLI** | Shell scripts, quick commands | `tbridge exec "ls -la"` |
| **REST API** | Ollama, LM Studio, HTTP clients | `POST localhost:9876/api/exec` |
| **Python SDK** | Custom automation | `from terminal_bridge import RemoteTerminal` |
| **MCP Server** | Cursor IDE agent | Auto-configured — just talk to the agent |
| **Stdio Pipe** | Claude CLI, aider | `echo '{"tool":"exec","command":"ls"}' \| tbridge pipe` |
| **Virtual Terminal** | Interactive SSH-like access | `tbridge connect --terminal` |

---

## Usage

### CLI

```bash
# One-shot commands
tbridge exec "brew install node"
tbridge exec "ls -la ~/projects"

# Persistent sessions (state carries over)
tbridge exec --session dev "cd /project"
tbridge exec --session dev "npm install"
tbridge exec --session dev "npm run build"

# File transfer
tbridge file push ./local-file.txt /remote/path/file.txt
tbridge file pull /remote/path/file.txt ./local-copy.txt

# Interactive terminal
tbridge connect --terminal
```

### REST API

```bash
curl -X POST http://127.0.0.1:9876/api/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "ls -la", "timeout": 30}'
```

### Python SDK

```python
from terminal_bridge import RemoteTerminal
import asyncio

async def main():
    async with RemoteTerminal("192.168.1.100") as remote:
        result = await remote.exec("uname -a")
        print(result["stdout"])

asyncio.run(main())
```

### Cursor (MCP)

After `tbridge setup local`, restart Cursor. The MCP server is auto-configured. Just ask the AI:

- *"Run `ls -la` on my remote Mac"*
- *"Install Node.js on the remote machine"*
- *"Create a new project folder on the remote"*

The agent uses the `execute_command` tool automatically.

---

## How It Works

1. **Remote Mac** runs a lightweight WebSocket agent (port 9877)
2. **Local Mac** connects and exposes the remote terminal through all six interfaces
3. **Any AI tool** on the local Mac can execute commands on the remote
4. **Auth**: HMAC-SHA256 challenge-response with keys in macOS Keychain
5. **Transport**: TLS-encrypted WebSocket connections

### Setup wizard handles everything

`tbridge setup remote` on the remote Mac:
- Generates a 256-bit secret key, stores it in Keychain
- Creates TLS certificates for encrypted connections
- Registers Bonjour/mDNS for LAN auto-discovery
- Installs a launchd service (auto-start on login)
- Configures macOS firewall
- Prints a pairing code + QR code

`tbridge setup local <CODE>` on the local Mac:
- Decodes the pairing code
- Stores the remote's key in Keychain
- Auto-discovers the remote via Bonjour
- Tests the connection
- Auto-configures Cursor MCP (`~/.cursor/mcp.json`)

---

## Internet Access

Terminal Bridge works over LAN by default. For access from anywhere:

### Tailscale (recommended — free)

Install [Tailscale](https://tailscale.com) on both Macs. Update the remote's IP in `~/.config/terminal-bridge/config.yaml` to the Tailscale IP. That's it — works from anywhere.

### Built-in Relay

For environments where Tailscale isn't an option, Terminal Bridge includes a relay server:

```bash
# On a VPS
pip install terminal-bridge
tbridge setup relay --port 9878
tbridge relay start --port 9878
```

Configure both Macs to connect through the relay. See the [full docs](https://github.com/rajeshrout97/terminal-bridge#internet-access) for details.

---

## Architecture

```
terminal-bridge/
├── src/terminal_bridge/
│   ├── cli.py                    # CLI entry point (tbridge)
│   ├── config.py                 # YAML config management
│   ├── protocol/messages.py      # 25+ Pydantic message types
│   ├── remote_agent/
│   │   ├── server.py             # WebSocket server (runs on remote)
│   │   ├── pty_manager.py        # macOS PTY management
│   │   └── auth.py               # HMAC challenge-response auth
│   ├── local_bridge/
│   │   ├── client.py             # WebSocket client + Python SDK
│   │   ├── mcp_server.py         # MCP server (9 tools for Cursor)
│   │   ├── rest_api.py           # REST API (12 endpoints)
│   │   ├── virtual_term.py       # Interactive terminal proxy
│   │   └── stdio_pipe.py         # JSON pipe for CLI tools
│   ├── relay/server.py           # Internet relay server
│   ├── security/
│   │   ├── tokens.py             # HMAC, rate limiting
│   │   ├── tls.py                # TLS certificate management
│   │   └── keychain.py           # macOS Keychain integration
│   └── setup/
│       ├── wizard.py             # Setup wizards
│       ├── pairing.py            # Pairing codes + QR display
│       ├── bonjour.py            # LAN auto-discovery
│       ├── launchd.py            # Background service management
│       ├── firewall.py           # macOS firewall setup
│       └── cursor_config.py      # Auto-configure Cursor MCP
└── tests/
```

### Security

- **Authentication**: HMAC-SHA256 challenge-response (no passwords over the wire)
- **Encryption**: TLS on all WebSocket connections
- **Key storage**: macOS Keychain (not plaintext files)
- **Rate limiting**: 5 auth attempts per minute per IP
- **REST API**: Bound to localhost only — not exposed to the network

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `tbridge setup remote` | Set up this Mac as a remote agent |
| `tbridge setup local <CODE>` | Pair with a remote Mac |
| `tbridge agent start\|stop\|status` | Manage the agent service |
| `tbridge exec <command>` | Run a command on the remote |
| `tbridge connect --terminal` | Interactive terminal session |
| `tbridge sessions list` | List active sessions |
| `tbridge file push\|pull` | Transfer files |
| `tbridge status` | Connection health |
| `tbridge pipe` | Stdio JSON pipe mode |
| `tbridge mcp` | Start MCP server |
| `tbridge api` | Start REST API server |
| `tbridge relay start` | Start relay server |

---

## Requirements

- **macOS 13+** (Ventura or later)
- **Python 3.10+**
- Both Macs on the same network (or use Tailscale/relay for internet)

---

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

```bash
git clone https://github.com/rajeshrout97/terminal-bridge.git
cd terminal-bridge
pip install -e ".[dev]"
pytest
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
