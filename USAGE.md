# Usage

This file describes how to use vBot in its current state.

vBot is a local-first, single-user system. The same runtime is exposed through
the server, WebUI, CLI, desktop shell, and channel integrations.

## 1. Requirements

- Python **3.11+**
- Node.js (not needed on hosts that use a prebuilt `webui/dist` via
  `--skip-webui-build`)
- at least one configured provider credential or OAuth connection

## 2. Setup

### Install with the script on Windows

The installer always builds the WebUI, installs the Python package in editable
mode, and prepares the data directory conservatively. If `~/.vbot/settings.json`
already exists and is valid JSON, it is kept as-is. If it is missing, the script
creates a minimal file with `server_port`. If it exists but is invalid, the
script stops instead of overwriting it. Existing port settings are respected for
installer start/autostart commands unless `-Port` is passed explicitly.

```powershell
.\scripts\install.ps1
```

By default the installer enables autostart (a Windows Task Scheduler task that
runs `vbot server start` at user login) and starts the server. Creating the task
needs an elevated PowerShell. Common options:

```powershell
.\scripts\install.ps1 -NoAutostart
.\scripts\install.ps1 -DataDir "$env:USERPROFILE\.vbot" -Port 8420
```

To uninstall the Python package while keeping the data directory untouched:

```powershell
.\scripts\uninstall.ps1
```

To also remove the optional autostart task:

```powershell
.\scripts\uninstall.ps1 -RemoveAutostart
```

### Install with the script on Linux

The Linux installer mirrors the Windows one: it installs the Python package in
editable mode, builds the WebUI, and prepares `~/.vbot` with the same
conservative rules (existing valid `settings.json` and `.env` are kept, invalid
`settings.json` stops the script, existing port settings are respected unless
`--port` is passed).

```bash
scripts/install.sh
```

By default the installer enables a systemd user autostart unit and starts the
server. Common options:

```bash
scripts/install.sh --no-autostart
scripts/install.sh --data-dir ~/.vbot --port 8420
scripts/install.sh --skip-webui-build
```

Notes:

- On PEP 668 systems (Debian, Raspberry Pi OS) the script must run inside a
  virtual environment. It fails early with instructions otherwise:

  ```bash
  python3 -m venv ~/vbot-venv
  source ~/vbot-venv/bin/activate
  scripts/install.sh
  ```

- Autostart (on by default; pass `--no-autostart` to skip) writes a systemd user
  unit to `~/.config/systemd/user/vbot.service` and enables login lingering so the
  server starts at boot, without root. Manage it with
  `systemctl --user status|start|stop vbot`.
- `--skip-webui-build` is for low-memory hosts (Pi 3 class) where `npm install`
  is not practical: build the WebUI on another machine
  (`cd webui && npm install && npm run build`) and copy `webui/dist` into the
  checkout first. On a Pi 5 the default on-device build is fine.

To uninstall the Python package while keeping the data directory untouched:

```bash
scripts/uninstall.sh
```

To also remove the systemd user unit:

```bash
scripts/uninstall.sh --remove-autostart
```

### Install Python dependencies

```bash
pip install -e ".[dev]"
```

### Install frontend dependencies

```bash
cd webui
npm install
cd ..
```

## 3. Data Directory and Configuration

By default vBot uses this data directory:

```text
~/.vbot
```

It contains, among other things:

- `.env` for API keys and tokens
- `settings.json` for instance settings
- `extensions/` for local Python hooks and extensions
- `agents/` for agent configurations and sessions
- `workspace-<agent-id>/` for agent workspaces
- `oauth/` for stored OAuth tokens
- `attachments/` for uploaded files
- `logs/` for daily log files
- `cron/` for persisted schedules

### Example `.env`

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

Port resolution order is:

1. `--port`
2. `VBOT_SERVER_PORT`
3. `settings.json`
4. `8420`

### Loading extensions and hooks

At startup vBot automatically scans:

```text
~/.vbot/extensions/
```

You can add more extension roots through `extension_directories` in
`settings.json`.

Supported entry-point forms for each direct child of an extension root:

- `~/.vbot/extensions/block_write.py`
- `~/.vbot/extensions/my_hooks/__init__.py`
- `~/.vbot/extensions/my_hooks/extension.py`

Important notes:

- Extension changes are loaded on the next server or runtime restart.
- Load failures log as `error`; handler failures log as `warn`. vBot continues fail-open.
- `register(api)` may be sync or async. Handlers may also be sync or async.

### Minimal extension example

For example, create:

```text
~/.vbot/extensions/block_write.py
```

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

- The **prompt block** adds a standing instruction to the System Prompt. It is
  positioned in the prompt layout and can be edited or disabled from the System
  Prompt tab; it renders only while this extension is loaded.
- `tool_call` intercepts every tool call.
- If the tool name is `write`, the extension returns `Deny`, so the real tool
  never runs and the model receives a `tool_call_denied` failure.

If you only want to rewrite parameters instead of blocking the call, return
`Modify` with the new input:

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

- To add standing System Prompt text, declare a **prompt block** with
  `api.register_prompt_block(...)` — there is no prompt-append hook.
- `context`: return a new message list when you only want to change the next model request.
- `tool_call`: return `None` (proceed), `Modify(input)` (rewrite arguments), `Deny(reason)` (block), or `Replace(result)` (substitute a result envelope).
- `tool_result`: return a full replacement result envelope to swap the result, or `None` to leave it unchanged (there is no shallow-merge patching).

## 4. Starting the Server

Foreground start:

```bash
python server/main.py
```

Managed background start via CLI:

```bash
python cli/main.py server start
```

Check status:

```bash
python cli/main.py server status
```

Stop the managed server:

```bash
python cli/main.py server stop
```

Restart the managed server:

```bash
python cli/main.py server restart
```

All other CLI areas are RPC-backed accessors. Commands such as `channel`,
`provider`, `model`, `skill`, and `config` require the target vBot server to be
running already; only `server start`, `server stop`, `server restart`, and
`server status` work without an already-running server.

Start on a custom port:

```bash
python server/main.py --port 9000
```

Start with a custom data directory:

```bash
python server/main.py --data-dir ./dev-data
```

Start with an explicit host and port:

```bash
python server/main.py --host 127.0.0.1 --port 8420
```

### Check whether the server is running

In a browser or via HTTP:

```text
http://127.0.0.1:8420/health
```

Expected response:

```json
{"status":"ok"}
```

## 5. Using the WebUI

For frontend development:

```bash
cd webui
npm run dev
```

Then open the local Vite URL printed by the command, usually something like:

```text
http://127.0.0.1:5173
```

To build the WebUI for the server to serve from `/`:

```bash
cd webui
npm run build
cd ..
```

Then open:

```text
http://127.0.0.1:8420/
```

The WebUI currently includes:

- Chat
- Agents
- Cron
- System Prompt
- Settings
- Logs

## 6. Using the Desktop Shell

The desktop app is a thin pywebview wrapper around the normal server-served
WebUI.

Start it with the default target:

```bash
python desktop/main.py
```

Or target a specific server:

```bash
python desktop/main.py --host 127.0.0.1 --port 8420
```

Important notes:

- The desktop shell does not start the server for you.
- It expects a reachable vBot server.
- If the server is healthy but has no built WebUI, the desktop shell stays open and shows an in-window message.

## 7. Managing Agents

The main user-facing path for agent management is the WebUI Agents view.

From there you can create, update, and delete agents, choose models and
connections, configure fallback models, toggle tools, and manage allowed
skills.

Each agent gets:

- `~/.vbot/agents/<agent-id>/agent.json`
- `~/.vbot/agents/<agent-id>/sessions/`
- `~/.vbot/workspace-<agent-id>/`

Built-in tools include `read`, `edit`, and `write`. Relative paths resolve from
the agent workspace; absolute paths are also allowed.

## 8. Using the Server RPC Directly

The server contract is available over HTTP, SSE, and WebSocket.

### 8.1 Create a session explicitly

PowerShell example:

Assumption: an agent with ID `coder` already exists.

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

### 8.2 Send one message and wait for the complete result

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

`chat.send` waits for the full Run to finish and then returns the complete
result.

### 8.3 Stream a Run

Start the Run first:

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

Then open the SSE stream, for example with `curl.exe`:

```powershell
curl.exe -N "$base$sseUrl"
```

Typical event blocks include:

- `run_started`
- `user_message_persisted`
- `reasoning`
- `tool_call_started`
- `tool_call_result`
- `assistant_output`
- `run_completed`

### 8.4 Cancel a running Run

```powershell
$cancelBody = @{
  method = "chat.cancel"
  params = @{
    run_id = $runId
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $cancelBody
```

Cancellation is best effort: vBot stops further execution as quickly as it can,
but already-running external work is not always hard-abortable.

## 9. WebSocket and Other Server Interfaces

App-wide server events are available at:

```text
ws://127.0.0.1:8420/ws
```

Live log streaming is available at:

```text
ws://127.0.0.1:8420/ws/logs
```

Attachment endpoints:

- `POST /api/upload`
- `GET /api/attachments/{attachment_id}`

## 10. CLI RPC Management

The CLI also exposes RPC-backed management commands through the running server.
This applies to channel, provider, model, skill, and config areas.

Examples:

```bash
python cli/main.py channel list
python cli/main.py provider list
python cli/main.py model list
python cli/main.py model refresh --provider openai
python cli/main.py skill list
python cli/main.py config
python cli/main.py config get server_port
python cli/main.py config set server_port 9000
python cli/main.py channel status --id my-channel
python cli/main.py channel enable --id my-channel
python cli/main.py channel disable --id my-channel
python cli/main.py channel remove --id my-channel
```

Adding a channel follows this shape:

```bash
python cli/main.py channel add --id my-channel --platform telegram --agent coder --token-env TELEGRAM_BOT_TOKEN
```

These commands require a reachable vBot server because they are RPC-backed accessors.

## 11. Frontend Build and Preview

Build the frontend:

```bash
cd webui
npm run build
```

Run the local preview server:

```bash
npm run preview
```

## 12. Quality Checks

Backend:

```bash
python scripts/quality.py
```

Frontend:

```bash
python scripts/quality-frontend.py
```

## 13. Home Assistant Integration

vBot can talk to your local Home Assistant instance through four LLM-callable
tools. They wrap HA's built-in REST API — no custom addons needed.

### Prerequisites

A **Long-Lived Access Token** from your Home Assistant profile page:

HA → your profile (bottom left) → Security → Long-Lived Access Tokens → Create Token

### Configuration

Add to `~/.vbot/.env`:

```env
HASS_TOKEN=eyJhbGciOi...   # your long-lived access token
HASS_URL=http://homeassistant.local:8123  # optional, this is the default
```

Without `HASS_TOKEN` the tools are **not registered** — they won't appear in
any agent's allowlist. Set the token and restart the server to activate them.

### The Four Tools

| Tool | What it does |
|---|---|
| `ha_list_entities` | List all entities, optionally filtered by domain or area |
| `ha_get_state` | Get the full state of a single entity |
| `ha_list_services` | Discover available services and their parameters |
| `ha_call_service` | Call a service (turn on a light, set temperature, etc.) |

### Example Session

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

Six HA domains are blocked on `ha_call_service` because they can execute
arbitrary code or make outbound HTTP requests: `shell_command`, `command_line`,
`python_script`, `pyscript`, `hassio`, and `rest_command`. All other domains
work normally. Entity IDs, domain names, and service names are validated
against Home Assistant's own format rules before any request is sent.

### Troubleshooting

- **Tools not showing up:** Make sure `HASS_TOKEN` is set and the server was
  restarted after adding it.
- **Connection refused:** Check that `HASS_URL` points to your HA instance and
  that HA is running.
- **401 Unauthorized:** The token is invalid or has been revoked. Create a new
  one in your HA profile.

## 14. Notes and Limitations

- A healthy server can exist without built WebUI assets. In that case `/health` works, but `/` may not.
- The CLI is automation-safe and does not open a browser.
- The desktop shell is only an accessor; it never manages the server process.
