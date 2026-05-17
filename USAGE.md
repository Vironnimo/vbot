# Usage

This file describes how to use vBot in its current state.

vBot is a local-first, single-user system. The same runtime is exposed through
the server, WebUI, CLI, desktop shell, and channel integrations.

## 1. Requirements

- Python **3.11+**
- Node.js
- at least one configured provider credential or OAuth connection

## 2. Setup

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
from core.tools.tools import tool_failure


def register(api):
  api.on("before_agent_start", append_rule)
  api.on("tool_call", block_write)


def append_rule(ctx, agent, session, messages, run):
  return {
    "system_prompt_append": (
      "Only edit files directly after you have first read or searched them."
    )
  }


def block_write(ctx, tool_name, tool_call_id, input):
  if tool_name != "write":
    return None

  return tool_failure(
    "tool_blocked",
    "The write tool is disabled in this instance by a local extension.",
  )
```

What this example does:

- `before_agent_start` appends text to the system prompt for the current Run.
- `tool_call` intercepts every tool call.
- If the tool name is `write`, the extension returns a failure envelope directly.
- That prevents the real tool from running.

If you only want to rewrite parameters instead of blocking the call, mutate
`input` in place and return `None`:

```python
def normalize_read_path(ctx, tool_name, tool_call_id, input):
  if tool_name == "read" and input.get("path") == "README":
    input["path"] = "README.md"
```

### Available hook events

- `run_start(ctx, session_id, agent_id)`
- `run_end(ctx, session_id, agent_id, outcome)` with `outcome = "success" | "error" | "cancelled"`
- `before_agent_start(ctx, agent, session, messages, run)`
- `context(ctx, messages)`
- `tool_call(ctx, tool_name, tool_call_id, input)`
- `tool_result(ctx, tool_name, tool_call_id, input, result)`

Most important return rules:

- `before_agent_start`: return `{"system_prompt_append": "..."}` to append text to the system prompt.
- `context`: return a new message list when you only want to change the next model request.
- `tool_call`: return a full tool-result envelope when you want to replace the tool call entirely.
- `tool_result`: return a patch dict; it is shallow-merged into the existing result envelope.

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

## 10. CLI Channel Management

The CLI also exposes channel-management commands through the running server.

Examples:

```bash
python cli/main.py channel list
python cli/main.py channel status --id my-channel
python cli/main.py channel enable --id my-channel
python cli/main.py channel disable --id my-channel
python cli/main.py channel remove --id my-channel
```

Adding a channel follows this shape:

```bash
python cli/main.py channel add --id my-channel --platform telegram --agent coder --token-env TELEGRAM_BOT_TOKEN
```

Channel commands require a reachable vBot server because they are RPC-backed.

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

## 13. Notes and Limitations

- A healthy server can exist without built WebUI assets. In that case `/health` works, but `/` may not.
- The CLI is automation-safe and does not open a browser.
- The desktop shell is only an accessor; it never manages the server process.
