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

## Requirements

- Python **3.11+**
- Node.js for WebUI development and builds

## Quick Start

### 1. Install Python dependencies

```bash
pip install -e ".[dev]"
```

### 2. Install WebUI dependencies

```bash
cd webui
npm install
cd ..
```

### 3. Add API keys

vBot reads configuration from `~/.vbot/` by default.

Create `~/.vbot/.env`, for example:

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ANTHROPIC_API_KEY=...
```

### 4. Start the server

Managed background start via CLI:

```bash
vbot server start
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
- CLI via `vbot ...`
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
