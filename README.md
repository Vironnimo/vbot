<h1 align="center">vBot</h1>

<p align="center"><i>Another personal AI agent. Supports your existing projects and their agents+skills, and the usual stuff.</i></p>

<p align="center">
  <img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache-2.0">
  <img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status: Alpha">
</p>

vBot is a personal AI agent you host yourself. You chat with it in the web UI — in your browser or through the bundled desktop app that wraps it — or from messaging apps like Telegram and Discord. It keeps persistent memory and skills and works with any model you choose. Point it at a project you already have and it picks up that project's agent team and skills, ready to run.

One async Python core runs everything; the web UI, desktop shell, CLI, and chat channels are just different ways to reach the same agents.

## Features

<table>
<tr><td><b>Talk to it your way</b></td><td>Chat in the web UI, in a bundled desktop app, or from Telegram and Discord — the same agent and the same memory on every surface.</td></tr>
<tr><td><b>Any model, any provider</b></td><td>OpenAI, Anthropic, OpenRouter, Ollama, Mistral, GitHub Copilot, and more. Pick a model per agent, with automatic fallback. No lock-in.</td></tr>
<tr><td><b>Brings your projects along</b></td><td>Point it at a repo you already have and it discovers that project's agent team and skills — OpenCode and Claude Code layouts both work — and runs them as they are.</td></tr>
<tr><td><b>Persistent by design</b></td><td>Every agent has its own workspace, curated long-term memory, and sessions that survive restarts.</td></tr>
<tr><td><b>Skills</b></td><td>Reusable playbooks the agent can load on demand — bundled, global, per-project, or ones the agent writes for itself.</td></tr>
<tr><td><b>Runs unattended</b></td><td>A built-in cron scheduler triggers agents on a schedule for reports, backups, and routine jobs.</td></tr>
<tr><td><b>Extensible</b></td><td>Local Python extensions and hooks can intercept tool calls and add prompt blocks. Home Assistant ships as a bundled extension.</td></tr>
<tr><td><b>Full API</b></td><td>HTTP RPC, Server-Sent Events streaming, and WebSocket events for your own integrations.</td></tr>
</table>

## Security

> **vBot runs AI agents with full access to the host it runs on.** By design, agents can read, write, and execute files, run arbitrary shell commands, edit vBot's own source, and trigger restarts. The server has **no authentication** — anyone who can reach its port can drive an agent with all of these capabilities.

This is safe only as a **local, single-user** tool bound to `127.0.0.1` (the default). Do **not**:

- bind the server to `0.0.0.0` or any public network interface,
- port-forward or reverse-proxy it to the internet without putting your own authentication in front of it,
- run it on a shared or untrusted host.

Treat exposing vBot to a network as granting remote code execution on that machine. API keys and bot tokens live in `~/.vbot/.env`, never in the repository — keep that directory private.

## Requirements

- Python **3.11+**
- Node.js for WebUI development and builds (not needed on hosts that use a prebuilt `webui/dist` via `--skip-webui-build`)

## Quick Start

The fastest path is the one-line bootstrap: it installs prerequisites (Python and git), clones the repo into `~/vbot`, installs into an isolated virtual environment (`~/vbot/.venv`), fetches the prebuilt WebUI, and puts `vbot` on your PATH (open a new terminal to use it). Your data lives separately in `~/.vbot`.

**Linux / Raspberry Pi:**

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/bootstrap.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/bootstrap.ps1 | iex
```

This installs the latest **release**, so no Node.js is needed on the machine, and enables autostart by default (pass `--no-autostart` to skip). On Windows, run the one-liner in an elevated PowerShell so the autostart task can be created — the install still succeeds otherwise. To pin a specific release instead of the latest, pass `--version v0.1.2` (Linux) or `-Version v0.1.2` (Windows); with the piped one-liner, append it after `bash -s --` (e.g. `… | bash -s -- --version v0.1.2`). To track `main` and build the WebUI locally instead, use the dev track: `bootstrap.sh --dev` on Linux, or download `bootstrap.ps1` and run it with `-Dev` on Windows. As always with `curl | bash` / `irm | iex`, download and read the script first if you prefer to review it before running.

To uninstall a bootstrap install, run its bundled uninstaller — it removes the whole `~/vbot` directory (virtual environment included), the `vbot` launcher, and the autostart entry, while leaving your data in `~/.vbot` untouched:

```bash
~/vbot/scripts/uninstall.sh
```

```powershell
& "$HOME\vbot\scripts\uninstall.ps1"
```

On Windows, run the uninstaller from an elevated PowerShell so the autostart task can be removed too.

Once installed, open the WebUI in your browser (default `http://127.0.0.1:8420/`), add at least one provider key, create an agent, and start chatting. See [USAGE.md](USAGE.md) for the full walkthrough.

## Manual Install

Prefer to clone the repo and run the installer yourself? On Windows, the installer prepares the Python CLI, builds the WebUI, and creates missing files in `~/.vbot` without overwriting an existing valid `settings.json` or `.env`:

```powershell
.\scripts\install.ps1
```

By default it enables autostart (a Windows Task Scheduler logon task) and starts the server. Creating the task needs an elevated (Administrator) PowerShell. Skip both with `-NoAutostart`. Uninstall removes the Python package only and leaves `~/.vbot` untouched (add `-RemoveAutostart` to also remove the task):

```powershell
.\scripts\uninstall.ps1
```

On Linux (e.g. a Raspberry Pi), the equivalent installer behaves the same way. On PEP 668 systems such as Debian and Raspberry Pi OS it must run inside a virtual environment and tells you how to create one otherwise. On low-memory hosts (Pi 3 class), skip the on-device WebUI build with `--skip-webui-build` and copy over a `webui/dist` built elsewhere.

```bash
scripts/install.sh
scripts/uninstall.sh
```

For a development checkout, install the package in editable mode and the WebUI dependencies:

```bash
pip install -e ".[dev]"
cd webui && npm ci && cd ..
```

Full options for every installer — data directory, ports, autostart, and desktop accessors — are documented in [USAGE.md](USAGE.md).

## Add API Keys

vBot reads configuration from `~/.vbot/` by default. Create `~/.vbot/.env`, for example:

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ANTHROPIC_API_KEY=...
```

Home Assistant ships as a bundled extension; configure it in Settings → Extensions instead of the `.env` file. See [USAGE.md](USAGE.md) for details.

## Start the Server

Managed background start via CLI:

```bash
python cli/main.py server start
```

Alternative foreground start:

```bash
python server/main.py
```

The default server URL is `http://127.0.0.1:8420`, and `http://127.0.0.1:8420/health` returns `{"status":"ok"}` once it is up.

## Open the UI

For WebUI development, run the Vite dev server and open the URL it prints:

```bash
cd webui
npm run dev
```

For the server-served WebUI, build once and open `http://127.0.0.1:8420/`:

```bash
cd webui
npm run build
cd ..
```

## Updating

Update an installed instance with:

```bash
vbot update
```

It updates the code from the git checkout it was installed from and restarts the server, without touching your data in `~/.vbot`. The installer records the exact dependency groups, Python interpreter, source track, and WebUI revision in the checkout, so `update` preserves server-only, server-plus-desktop, and Desktop Client installations instead of guessing from imports. A release install fetches the latest release and its prebuilt WebUI; a `main` (dev) install pulls and rebuilds the WebUI locally. Failed dependency, WebUI, or stash steps remain retryable even when Git already advanced, and a release update checks that the required WebUI asset exists before changing the checkout. If you have local changes to tracked files, `update` stops — re-run with `--discard` to drop them or `--stash` to keep them (reapplied after, including when the update fails). Use `--no-restart` to update without restarting; Desktop Client updates never try to restart a server.

## Default Data Directory

By default vBot stores runtime data under `~/.vbot`. This includes, among other things:

- `.env` for API keys and tokens
- `settings.json` for instance settings
- `agents/<agent-id>/` for each agent's config, sessions, and workspace
- `extensions/` for local Python hooks
- `oauth/` for OAuth tokens
- `attachments/` for uploaded blobs
- `logs/` for daily log files
- `cron/` for persisted schedules

## Access Paths

- WebUI in the browser
- Desktop shell via `python desktop/main.py`
- Messaging channels (Telegram, Discord)
- CLI via `python cli/main.py ...` for server lifecycle and RPC-backed management
- HTTP, SSE, and WebSocket integrations against the server

## Server Interfaces

The server exposes:

- `POST /api/rpc` for the RPC API
- `GET /api/runs/{run_id}/events` for one Run's SSE stream
- `GET /ws` for app-wide server events
- `GET /ws/logs` for live log streaming
- `POST /api/upload` for attachment uploads
- `GET /api/attachments/{attachment_id}` for attachment downloads
- `GET /health` for server health

## Documentation

- [USAGE.md](USAGE.md) — detailed setup, configuration, extensions, RPC examples, and workflows

## Quality Checks

```bash
python scripts/quality.py            # backend
python scripts/quality-frontend.py   # frontend
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
