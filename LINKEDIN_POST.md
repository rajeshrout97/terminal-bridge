# LinkedIn Post Draft

---

I built Terminal Bridge — an open-source tool that lets any AI agent control a remote Mac's terminal over the network.

The problem: I have two Macs. My AI tools (Cursor, Claude CLI, Ollama) run on one. But I often need them to execute commands on the other — install packages, run builds, manage services. SSH works, but AI tools don't natively speak SSH.

Terminal Bridge sits between them. Install it on both Macs, run one setup command, and your AI agent can execute commands on the remote machine as naturally as it does locally.

What makes it different:
- Model-agnostic — works with Cursor, Claude CLI, Ollama, LM Studio, aider, or any AI tool
- Six interfaces — CLI, REST API, Python SDK, MCP server, stdio pipe, interactive terminal
- Secure by default — HMAC-SHA256 auth, TLS encryption, macOS Keychain for key storage
- Zero config for Cursor — auto-configures the MCP server. Restart Cursor and go.
- Three commands to set up: `pip install terminal-bridge`, `tbridge setup remote`, `tbridge setup local <code>`

It's free, open source (MIT), and on PyPI:

pip install terminal-bridge

GitHub: https://github.com/rajeshrout97/terminal-bridge
PyPI: https://pypi.org/project/terminal-bridge/
Landing page: https://rajeshrout97.github.io/terminal-bridge/

If you work with multiple Macs and AI tools, give it a try. PRs and feedback welcome.

#OpenSource #AI #MacOS #DeveloperTools #Python #MCP #Cursor #RemoteTerminal

---
