# Usage

This guide covers how to install, configure, run, and integrate with vBot in its current state. vBot is a single-user system you run yourself: one runtime is exposed through the server, the WebUI, the CLI, the desktop shell, and chat channels.

For a high-level overview and the fastest one-line install, see the [README](README.md). This document is the detailed reference.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
  - [Windows installer](#windows-installer)
  - [Linux installer](#linux-installer)
  - [Desktop add-ons](#desktop-add-ons)
  - [Manual development install](#manual-development-install)
  - [Uninstalling](#uninstalling)
- [Updating](#updating)
- [Data Directory and Configuration](#data-directory-and-configuration)
- [Running the Server](#running-the-server)
- [Using the WebUI](#using-the-webui)
- [Using the Desktop Shell](#using-the-desktop-shell)
- [Managing Agents and Projects](#managing-agents-and-projects)
- [Channels](#channels)
- [Extensions and Hooks](#extensions-and-hooks)
- [Home Assistant Integration](#home-assistant-integration)
- [CLI Management Commands](#cli-management-commands)
- [Server API (HTTP, SSE, WebSocket)](#server-api-http-sse-websocket)
- [Frontend Build and Preview](#frontend-build-and-preview)
- [Quality Checks](#quality-checks)
- [Notes and Limitations](#notes-and-limitations)

## Requirements

- Python **3.11+**
- Node.js — for WebUI development and builds (not needed on hosts that use a prebuilt `webui/dist` via `--skip-webui-build`)
- At least one configured provider credential or OAuth connection

## Installation

The quickest path is the one-line bootstrap documented in the [README](README.md#quick-start). The script installers below give you full control over data directory, port, autostart, and desktop accessors. Both installers are conservative: they never overwrite an existing valid `settings.json` or `.env`, they respect existing port settings unless a port is passed explicitly (an explicit port is then also written into `settings.json`, so autostart and later flag-less commands resolve the same port), and they stop rather than clobber an invalid `settings.json`. A successful install also writes `.vbot-install.json` inside the checkout. This checkout-local state records the exact dependency groups, Python interpreter, source track, applied revision, and WebUI revision used by update and uninstall; it contains no credentials or runtime data.

### Windows installer

The installer builds the WebUI, installs the Python package in editable mode, and prepares the data directory. If `~/.vbot/settings.json` already exists and is valid JSON, it is kept as-is; if it is missing, a minimal file with `server_port` is created; if it exists but is invalid, the script stops instead of overwriting it.

```powershell
.\scripts\install.ps1
```

By default the installer enables autostart (a Windows Task Scheduler task that runs `vbot server start` at user login) and starts the server. Creating the task needs an elevated PowerShell. Common options:

```powershell
.\scripts\install.ps1 -NoAutostart
.\scripts\install.ps1 -DataDir "$env:USERPROFILE\.vbot" -Port 8420
```

### Linux installer

The Linux installer mirrors the Windows one: it installs the Python package in editable mode, builds the WebUI, and prepares `~/.vbot` with the same conservative rules.

```bash
scripts/install.sh
```

By default it enables a systemd user autostart unit and starts the server. Common options:

```bash
scripts/install.sh --no-autostart
scripts/install.sh --data-dir ~/.vbot --port 8420
scripts/install.sh --skip-webui-build
```

Notes:

- On PEP 668 systems (Debian, Raspberry Pi OS) the script must run inside a virtual environment. It fails early with instructions otherwise:

  ```bash
  python3 -m venv ~/vbot-venv
  source ~/vbot-venv/bin/activate
  scripts/install.sh
  ```

- Autostart (on by default; pass `--no-autostart` to skip) writes a systemd user unit to `~/.config/systemd/user/vbot.service` and enables login lingering so the server starts at boot, without root. The unit quotes all executable and path arguments, and custom service names must start with a letter or number, then use only letters, numbers, `.`, `_`, `@`, and `-`, without a `.service` suffix. If login lingering cannot be enabled, the installer reports that boot-before-login is not guaranteed instead of claiming full success. The unit uses `KillMode=process`, so an agent-triggered `vbot server restart` survives the unit's own shutdown. Manage it with `systemctl --user status|start|stop vbot`.
- `--skip-webui-build` is for low-memory hosts (Pi 3 class) where `npm ci` is not practical: build the WebUI on another machine (`cd webui && npm ci && npm run build`) and copy `webui/dist` into the checkout first. On a Pi 5 the default on-device build is fine.

### Desktop add-ons

The desktop shell (a pywebview window around the WebUI) is optional. Two mutually exclusive install shapes add it:

- **Add the desktop accessor to a full server install** — installs the desktop dependencies alongside the server and creates a launcher for `vbot desktop`.

  ```powershell
  .\scripts\install.ps1 -Desktop
  ```

  ```bash
  scripts/install.sh --desktop
  ```

- **Install the desktop client alone** — no server stack, no local WebUI build, no data-directory setup, no autostart. For a machine that connects to a *remote* vBot server (for example a Raspberry Pi on your network).

  ```powershell
  .\scripts\install.ps1 -DesktopClient
  ```

  ```bash
  scripts/install.sh --desktop-client
  ```

On Windows a Start-menu shortcut ("vBot Desktop") is created; on Linux a freedesktop application-menu entry (`~/.local/share/applications/vbot-desktop.desktop`) is written. A development install (`-Dev` / `.[dev]`) composes with `-Desktop` but is rejected together with `-DesktopClient`.

### Manual development install

Install the package in editable mode with the development extras, then the WebUI dependencies:

```bash
pip install -e ".[dev]"
```

```bash
cd webui
npm ci
cd ..
```

### Uninstalling

Uninstall is intentionally data-directory preserving — it never removes `~/.vbot`. It uses the Python interpreter recorded by the installer rather than whichever `python` happens to be first on the current PATH, then removes the checkout-local install state after a successful package removal.

```powershell
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -RemoveAutostart
```

```bash
scripts/uninstall.sh
scripts/uninstall.sh --remove-autostart
```

Pass the autostart flag to also remove the Task Scheduler task (Windows) or the systemd user unit (Linux). A bootstrap install has its own bundled uninstaller that removes the whole `~/vbot` tree; see the [README](README.md#quick-start).

## Updating

Run `vbot update` from an installed checkout. The updater reads `.vbot-install.json` and preserves the exact installation: `server`, `server-desktop`, or `desktop-client`, together with its recorded dependency groups and Python interpreter. Older checkouts without the file are inferred once and then persisted.

Release installs preflight the matching `webui-dist.tar.gz` before moving to a newer tag; bootstrap waits briefly for that asset when a freshly published release is still assembling. Development installs use `npm ci` and rebuild only when the recorded WebUI revision requires it. Dependency and WebUI completion are recorded separately, so a failed step can be retried even when Git already reached the target revision. A staged WebUI replacement never removes the last working `webui/dist` when extraction or swapping fails.

Local tracked changes still require an explicit choice: `--discard` drops them, while `--stash` restores them after both successful and failed update attempts. A stash conflict is kept for manual recovery and is reported without pretending the server restarted. Use `--no-restart` to update a server installation without restarting it. Desktop Client installations skip WebUI management and server restart entirely.

## Data Directory and Configuration

By default vBot uses this data directory:

```text
~/.vbot
```

It contains, among other things:

- `.env` for API keys and tokens
- `settings.json` for instance settings
- `extensions/` for local Python hooks and extensions
- `agents/<agent-id>/` for each agent's configuration, sessions, and workspace
- `projects/<project-id>/` for registered projects (their sessions and per-agent anchors)
- `oauth/` for stored OAuth tokens
- `attachments/` for uploaded files
- `logs/` for daily log files
- `cron/` for persisted schedules

### Example `.env`

The `.env` belongs to you and is read at startup as a fallback credential source. Process environment variables take precedence over it.

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ANTHROPIC_API_KEY=...
```

### Example `settings.json`

```json
{
  "server_port": 8420,
  "extension_directories": [
    "~/vbot-exts"
  ]
}
```

### Port resolution order

1. `--port`
2. `VBOT_SERVER_PORT`
3. `settings.json`
4. `8420`

## Running the Server

Only server lifecycle commands act locally; every other CLI area needs a running server (see [CLI Management Commands](#cli-management-commands)).

Foreground start:

```bash
python server/main.py
```

Managed background start via CLI:

```bash
python cli/main.py server start
```

Check status, stop, or restart the managed server:

```bash
python cli/main.py server status
python cli/main.py server stop
python cli/main.py server restart
```

Start on a custom port, a custom data directory, or an explicit host and port:

```bash
python server/main.py --port 9000
python server/main.py --data-dir ./dev-data
python server/main.py --host 127.0.0.1 --port 8420
```

### Health check

In a browser or via HTTP:

```text
http://127.0.0.1:8420/health
```

Expected response:

```json
{"status":"ok"}
```

## Using the WebUI

For frontend development, run the Vite dev server and open the local URL it prints (usually `http://127.0.0.1:5173`):

```bash
cd webui
npm run dev
```

To build the WebUI for the server to serve from `/`, build once and open `http://127.0.0.1:8420/`:

```bash
cd webui
npm run build
cd ..
```

The WebUI currently includes these views:

- **Chat** — talk to an agent, stream responses, run slash commands
- **Agents** — create and configure agents
- **Projects** — register project directories and inspect their teams
- **System Prompt** — edit and reorder the prompt blocks
- **Cron** — schedule unattended runs
- **Statistics** — token and run aggregates over your sessions
- **Settings** — instance settings, providers, extensions
- **Logs** — live and historical log viewing

A **Debug** view additionally appears when Debug Mode is enabled in Settings.

## Using the Desktop Shell

The desktop app is a thin pywebview wrapper around the normal server-served WebUI. Start it with the default target, or point it at a specific server:

```bash
python desktop/main.py
python desktop/main.py --host 127.0.0.1 --port 8420
```

Important notes:

- The desktop shell does not start the server for you; it expects a reachable vBot server.
- If it cannot reach a server, it shows an in-window connection screen listing remembered servers, rather than a dead end.
- If the server is healthy but has no built WebUI, the desktop shell stays open and shows an in-window message.

## Managing Agents and Projects

The main user-facing path for agent management is the WebUI **Agents** view. From there you can create, update, and delete agents, choose models and connections, configure fallback models, toggle tools, and manage allowed skills.

Each agent gets its own directory:

- `~/.vbot/agents/<agent-id>/agent.json`
- `~/.vbot/agents/<agent-id>/sessions/`
- `~/.vbot/agents/<agent-id>/workspace/`

Built-in tools include `read`, `edit`, and `write`. Relative paths resolve against the **cwd** — the project repository for a project session, otherwise the agent's workspace. Absolute paths bypass this and are also allowed.

### Projects and teams

A project bundles a working directory (a repository) with the agent team and skills defined inside it. Register a project through the WebUI **Projects** view or the `vbot project` commands, and vBot discovers that project's team by scanning its declared layout — OpenCode (`.opencode/agents/`, `.opencode/skills/`) or Claude Code (`.claude/agents/`, `.claude/skills/`). The repository is the source of truth; vBot reads it but never writes to it. The project's runtime data (its sessions) lives in the data directory under `projects/<project-id>/`.

## Channels

vBot can talk to messaging platforms so you can reach an agent from Telegram or Discord. Channels are managed through the CLI (or the equivalent RPC). Adding a channel follows this shape:

```bash
python cli/main.py channel add --id my-channel --platform telegram --agent coder --token-env TELEGRAM_BOT_TOKEN
```

The `--token-env` value names an environment variable (typically set in `~/.vbot/.env`) that holds the bot token. Manage a channel's lifecycle with:

```bash
python cli/main.py channel list
python cli/main.py channel status --id my-channel
python cli/main.py channel enable --id my-channel
python cli/main.py channel disable --id my-channel
python cli/main.py channel remove --id my-channel
```

## Extensions and Hooks

At startup vBot automatically scans `~/.vbot/extensions/`. You can add more extension roots through `extension_directories` in `settings.json`.

Supported entry-point forms for each direct child of an extension root:

- `~/.vbot/extensions/block_write.py`
- `~/.vbot/extensions/my_hooks/__init__.py`
- `~/.vbot/extensions/my_hooks/extension.py`

Important notes:

- Extension changes are loaded on the next server or runtime restart (or via an explicit extension reload).
- Load failures log as `error`; handler failures log as `warn`. vBot continues fail-open.
- `register(api)` may be sync or async. Handlers may also be sync or async.

### Minimal extension example

Create `~/.vbot/extensions/block_write.py`:

```python
from core.extensions import Deny


def register(api):
  # Add standing System Prompt text as a prompt block (not a hook).
  api.register_prompt_block(
    "edit_discipline",
    default_text="Only edit files directly after you have first read or searched them.",
  )
  api.on("tool_call", block_write)


def block_write(ctx, tool_name, tool_call_id, input):
  if tool_name != "write":
    return None

  return Deny(reason="The write tool is disabled in this instance by a local extension.")
```

What this example does:

- The **prompt block** adds a standing instruction to the System Prompt. It is positioned in the prompt layout and can be edited or disabled from the System Prompt tab; it renders only while this extension is loaded.
- `tool_call` intercepts every tool call. If the tool name is `write`, the extension returns `Deny`, so the real tool never runs and the model receives a `tool_call_denied` failure.

If you only want to rewrite parameters instead of blocking the call, return `Modify` with the new input:

```python
from core.extensions import Modify


def normalize_read_path(ctx, tool_name, tool_call_id, input):
  if tool_name == "read" and input.get("path") == "README":
    return Modify({**input, "path": "README.md"})
  return None
```

### Available hook events

- `run_start(ctx, session_id, agent_id)`
- `run_end(ctx, session_id, agent_id, outcome)` with `outcome = "success" | "error" | "cancelled"`
- `context(ctx, messages)`
- `tool_call(ctx, tool_name, tool_call_id, input)`
- `tool_result(ctx, tool_name, tool_call_id, input, result)`

Most important return rules:

- To add standing System Prompt text, declare a **prompt block** with `api.register_prompt_block(...)` — there is no prompt-append hook.
- `context`: return a new message list when you only want to change the next model request.
- `tool_call`: return `None` (proceed), `Modify(input)` (rewrite arguments), `Deny(reason)` (block), or `Replace(result)` (substitute a result envelope).
- `tool_result`: return a full replacement result envelope to swap the result, or `None` to leave it unchanged (there is no shallow-merge patching).

## Home Assistant Integration

vBot can talk to your local Home Assistant instance through four LLM-callable tools. They wrap HA's built-in REST API — no custom add-ons needed. Home Assistant ships as a **bundled extension**, active out of the box and configured in the UI.

### Prerequisites

A **Long-Lived Access Token** from your Home Assistant profile page:

HA → your profile (bottom left) → Security → Long-Lived Access Tokens → Create Token

### Configuration

Open **Settings → Extensions → Home Assistant** and fill in two fields:

- **Server URL** — the base URL of your Home Assistant instance (defaults to `http://homeassistant.local:8123`).
- **Access token** — paste your long-lived access token. This is a write-only secret field: it is stored in `~/.vbot/.env` under `HASS_TOKEN` and never shown back. An existing `HASS_TOKEN` in `.env` keeps working unchanged.

Both values take effect immediately — no restart. Until a token is set, the four tools stay hidden everywhere (prompt, provider tools, and every tool picker), and the Extensions tab shows Home Assistant as loaded and waiting for a token.

### The four tools

| Tool | What it does |
|---|---|
| `ha_list_entities` | List all entities, optionally filtered by domain or area |
| `ha_get_state` | Get the full state of a single entity |
| `ha_list_services` | Discover available services and their parameters |
| `ha_call_service` | Call a service (turn on a light, set temperature, etc.) |

### Example session

```
User: What lights are on right now?

Agent calls ha_list_entities with domain=light
→ Light.living_room: on, Light.kitchen: off

Agent: The living room light is on. The kitchen light is off.

User: Turn off the living room light.

Agent calls ha_list_services with domain=light
→ sees turn_off service

Agent calls ha_call_service with domain=light, service=turn_off,
     entity_id=light.living_room
→ success

Agent: Done. The living room light is now off.
```

### Security

Six HA domains are blocked on `ha_call_service` because they can execute arbitrary code or make outbound HTTP requests: `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, and `rest_command`. All other domains work normally. Entity IDs, domain names, and service names are validated against Home Assistant's own format rules before any request is sent.

### Troubleshooting

- **Tools not showing up:** Make sure the access token is set in Settings → Extensions → Home Assistant. Without it the tools stay hidden; the Extensions tab shows the extension waiting for a token.
- **Connection refused:** Check that the Server URL in Settings → Extensions points to your HA instance and that HA is running.
- **401 Unauthorized:** The token is invalid or has been revoked. Create a new one in your HA profile and paste it into the token field.

## CLI Management Commands

Beyond server lifecycle, the CLI exposes RPC-backed management commands through the running server. These cover channel, provider, model, skill, project, config, and other areas. They require a reachable vBot server because they are RPC-backed accessors.

```bash
python cli/main.py provider list
python cli/main.py model list
python cli/main.py model refresh --provider openai
python cli/main.py skill list
python cli/main.py project list
python cli/main.py config
python cli/main.py config get server_port
python cli/main.py config set server_port 9000
```

CLI output is agent-facing: success, failure, help text, and suggestions are explicit enough to choose the next command without guessing.

## Server API (HTTP, SSE, WebSocket)

The server contract is available over HTTP, SSE, and WebSocket. The examples below use PowerShell and assume an agent with ID `coder` already exists.

### Create a session

```powershell
$base = "http://127.0.0.1:8420"

$createBody = @{
  method = "session.create"
  params = @{
    agent_id = "coder"
  }
} | ConvertTo-Json -Depth 5

$sessionResponse = Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $createBody

$sessionId = $sessionResponse.result.session_id
$sessionId
```

### Send one message and wait for the result

`chat.send` waits for the full Run to finish and then returns the complete result.

```powershell
$sendBody = @{
  method = "chat.send"
  params = @{
    agent_id = "coder"
    session_id = $sessionId
    content = "Say hello in one short sentence."
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $sendBody
```

### Stream a Run

Start the Run, then open its SSE stream:

```powershell
$streamBody = @{
  method = "chat.stream"
  params = @{
    agent_id = "coder"
    session_id = $sessionId
    content = "Explain in two sentences what vBot is."
  }
} | ConvertTo-Json -Depth 5

$streamResponse = Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $streamBody

$runId = $streamResponse.result.run_id
$sseUrl = $streamResponse.result.sse_url
```

```powershell
curl.exe -N "$base$sseUrl"
```

Typical event blocks include `run_started`, `user_message_persisted`, `reasoning`, `tool_call_started`, `tool_call_result`, `assistant_output`, and `run_completed`.

### Cancel a running Run

Cancellation is best effort: vBot stops further execution as quickly as it can, but already-running external work is not always hard-abortable.

```powershell
$cancelBody = @{
  method = "chat.cancel"
  params = @{
    run_id = $runId
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $cancelBody
```

### Other interfaces

App-wide server events and live log streaming are available over WebSocket, and attachments have dedicated HTTP endpoints:

```text
ws://127.0.0.1:8420/ws
ws://127.0.0.1:8420/ws/logs
```

- `POST /api/upload`
- `GET /api/attachments/{attachment_id}`

## Frontend Build and Preview

Build the frontend and run the local preview server:

```bash
cd webui
npm run build
npm run preview
```

## Quality Checks

Both gates run format → lint → type-check → test (and build, for the frontend). Pass paths to scope them, or no arguments for a full scan.

```bash
python scripts/quality.py            # backend
python scripts/quality-frontend.py   # frontend
```

## Notes and Limitations

- A healthy server can exist without built WebUI assets. In that case `/health` works, but `/` may not.
- The CLI is automation-safe and does not open a browser.
- The desktop shell is only an accessor; it never manages the server process.
