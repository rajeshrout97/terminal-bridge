# LinkedIn Post

---

I built Terminal Bridge — an open-source tool that lets any AI agent control a remote Mac's terminal over the network.

One runs my AI tools — Cursor, Claude CLI, Ollama. The other is where I actually need commands to run — builds, installs, deploys.

The problem? AI agents don't speak SSH. Cursor can't natively run a command on another machine. Neither can Ollama or Claude CLI. I kept copy-pasting terminal output between machines like it was 2005.

I looked for a solution. There isn't one.

No tool exists that lets any AI agent talk to a remote Mac's terminal. Not model-specific. Not protocol-specific. Just... nothing.

So I built one.

Terminal Bridge — open source, three commands to set up:

pip install terminal-bridge
tbridge setup remote        (on the Mac you want to control)
tbridge setup local <CODE>  (on the Mac where your AI runs)

That's it. Your AI agent can now execute commands on the remote Mac as naturally as it does locally.

What makes it different from just "using SSH":

→ Model-agnostic. Cursor, Claude CLI, Ollama, LM Studio, aider — anything that speaks HTTP, MCP, or stdio.
→ Six interfaces. CLI, REST API, Python SDK, MCP server for Cursor, stdio pipe, interactive terminal.
→ Secure by default. HMAC-SHA256 auth, TLS encryption, keys in macOS Keychain — not plaintext files.
→ Zero config for Cursor. The setup wizard auto-configures MCP. Restart Cursor and go.
→ Runs as a background service. Survives reboots. Zero daily maintenance.

This doesn't exist in the market. If you work with multiple Macs and AI coding tools, this saves you real time every single day.

It's free, MIT licensed, and live on PyPI right now.

GitHub: https://github.com/rajeshrout97/terminal-bridge
PyPI: https://pypi.org/project/terminal-bridge/
Landing page: https://rajeshrout97.github.io/terminal-bridge/

Give it a try. Star it if it's useful. PRs and feedback are very welcome — this is v0.1.0 and I'm building in public.

#OpenSource #AI #MacOS #DeveloperTools #Python #MCP #CursorAI #BuildInPublic

---
