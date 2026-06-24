# vBot

vBot is a local-first agent harness: an async Python runtime that gives AI
agents their own workspace, persistent state, tool access, and multiple ways to
interact with the same system.

The project currently includes:

- a provider and model registry with multiple connection types
- persistent agents, sessions, workspaces, attachments, and logs
- an agentic chat loop with tool support, streaming, slash commands, and model fallback
- a FastAPI server with RPC, Server-Sent Events (SSE), WebSocket events, log streaming, and attachment endpoints
- a Svelte WebUI with Chat, Agents, Cron, System Prompt, Settings, and Logs views
- a CLI for local server lifecycle management and RPC-backed management commands
- a pywebview desktop shell that loads the normal server-served WebUI
- local extensions, skills, cron jobs, and channel integrations

vBot is designed as a local, single-user system. The server is the shared core;
the WebUI, CLI, desktop shell, and channel adapters are accessors around it.
Except for `server start`, `server stop`, `server restart`, and `server status`,
CLI commands require a running vBot server and go through its RPC surface.

## Security

> **vBot runs AI agents with full access to the host it runs on.** By design, agents can read, write, and execute files, run arbitrary shell commands, edit vBot's own source, and trigger restarts. The server has **no authentication** — anyone who can reach its port can drive an agent with all of these capabilities.

This is safe only as a **local, single-user** tool bound to `127.0.0.1` (the default). Do **not**:

- bind the server to `0.0.0.0` or any public network interface,
- port-forward or reverse-proxy it to the internet without putting your own authentication in front of it,
- run it on a shared or untrusted host.

Treat exposing vBot to a network as granting remote code execution on that machine. API keys and bot tokens live in `~/.vbot/.env`, never in the repository — keep that directory private.

## Requirements

- Python **3.11+**
- Node.js for WebUI development and builds (not needed on hosts that use a
  prebuilt `webui/dist` via `--skip-webui-build`)

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

The numbered steps below describe the manual install if you would rather clone the repo and run the installer yourself.

### 1. Install vBot

On Windows, the installer prepares the Python CLI, builds the WebUI, and creates
missing files in `~/.vbot` without overwriting an existing valid
`settings.json` or `.env`:

```powershell
.\scripts\install.ps1
```

By default the installer enables autostart (a Windows Task Scheduler logon task) and starts the server. Creating the task needs an elevated (Administrator) PowerShell. To skip autostart and the immediate start:

```powershell
.\scripts\install.ps1 -NoAutostart
```

Uninstall removes the Python package only. It leaves `~/.vbot` untouched:

```powershell
.\scripts\uninstall.ps1
```

Remove the optional autostart task too:

```powershell
.\scripts\uninstall.ps1 -RemoveAutostart
```

On Linux (e.g. a Raspberry Pi), the equivalent installer behaves the same way.
On PEP 668 systems such as Debian and Raspberry Pi OS it must run inside a
virtual environment and tells you how to create one otherwise:

```bash
scripts/install.sh
```

By default it enables a systemd user autostart unit (started at boot via login lingering) and starts the server. To skip that:

```bash
scripts/install.sh --no-autostart
```

On low-memory hosts (Pi 3 class), skip the on-device WebUI build and use a
`webui/dist` built on another machine and copied over:

```bash
scripts/install.sh --skip-webui-build
```

Uninstall mirrors the Windows script and leaves `~/.vbot` untouched:

```bash
scripts/uninstall.sh
scripts/uninstall.sh --remove-autostart
```

### Manual development install

```bash
pip install -e ".[dev]"
```

### Install WebUI dependencies

```bash
cd webui
npm install
cd ..
```

### Add API keys

vBot reads configuration from `~/.vbot/` by default.

Create `~/.vbot/.env`, for example:

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ANTHROPIC_API_KEY=...
```

### 3b. Home Assistant (optional)

Add to `~/.vbot/.env`:

```env
HASS_TOKEN=...          # Long-Lived Access Token from your HA profile
HASS_URL=http://homeassistant.local:8123  # optional, this is the default
```

With a valid token, vBot registers 4 LLM-callable tools: `ha_list_entities`,
`ha_get_state`, `ha_list_services`, and `ha_call_service`.

### 4. Start the server

Managed background start via CLI:

```bash
python cli/main.py server start
```

Alternative foreground start:

```bash
python server/main.py
```

Default server URL:

```text
http://127.0.0.1:8420
```

Health check:

```text
http://127.0.0.1:8420/health
```

### 5. Open the UI

For WebUI development:

```bash
cd webui
npm run dev
```

Open the local Vite URL printed by the command.

For the server-served WebUI:

```bash
cd webui
npm run build
cd ..
```

Then open:

```text
http://127.0.0.1:8420/
```

## Updating

Update an installed instance with:

```bash
vbot update
```

It updates the code from the git checkout it was installed from and restarts the server, without touching your data in `~/.vbot`. A release install fetches the latest release and its prebuilt WebUI; a `main` (dev) install pulls and rebuilds the WebUI locally. If you have local changes to tracked files, `update` stops — re-run with `--discard` to drop them or `--stash` to keep them (reapplied after). Use `--no-restart` to update without restarting.

## Default Data Directory

By default vBot stores runtime data under:

```text
~/.vbot
```

This includes, among other things:

- `.env` for API keys and tokens
- `settings.json` for instance settings
- `agents/` for agent configs and sessions
- `workspace-<agent-id>/` for agent workspaces
- `extensions/` for local Python hooks
- `oauth/` for OAuth tokens
- `attachments/` for uploaded blobs
- `logs/` for daily log files
- `cron/` for persisted schedules

## Server Interfaces

The current server exposes:

- `POST /api/rpc` for the RPC API
- `GET /api/runs/{run_id}/events` for one Run SSE stream
- `GET /ws` for app-wide server events
- `GET /ws/logs` for live log streaming
- `POST /api/upload` for attachment uploads
- `GET /api/attachments/{attachment_id}` for attachment downloads
- `GET /health` for server health

## Main Access Paths

- WebUI in the browser
- CLI via `python cli/main.py ...`
- Desktop shell via `python desktop/main.py`
- HTTP, SSE, and WebSocket integrations against the server

## Documentation

- `USAGE.md` for detailed setup and workflows

## Quality Checks

Backend:

```bash
python scripts/quality.py
```

Frontend:

```bash
python scripts/quality-frontend.py
```
