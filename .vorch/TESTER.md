# Live Testing Guide

Project-specific instructions for testing the running vBot application via CLI
and browser. This file is for the Tester agent — not for writing unit or
integration tests (that is the Builder's job with pytest/Vitest).

## Prerequisites

Before starting any live test session:

1. The current Python interpreter already has vBot installed
2. No other process on the target port (default 8420)

If the environment is not ready, report **Blocked** — do not attempt to install
packages or set up the environment yourself.

## Starting the App

Run the test-env script. It rebuilds the frontend, starts the server, and
waits until the health check passes:

```bash
python scripts/test-env.py start
```

Options: `--host`, `--port`, `--data-dir`.

On success it prints the resolved URL and WebUI status. Use that URL for all
browser testing. **Default URL: `http://localhost:8420`**

If it fails, read the output and report **Blocked** with the error details.

## Stopping the App

```bash
python scripts/test-env.py stop
```

Always close browser sessions (`playwright-cli close`) before stopping the
server. No orphan processes.

## CLI Commands to Test

All commands accept `--host`, `--port`, `--data-dir` flags. Run via
`python cli/main.py server <command>`.

| Command | What it does | What to verify |
|---|---|---|
| `server start` | Starts server if not running | Reports "started"; reports "already running" if vBot is at target; reports conflict if a non-vBot process occupies the port |
| `server stop` | Stops a running server | Reports "stopped"; reports "not running" if already stopped; reports conflict if non-vBot process is on the port |
| `server restart` | Stop then start | Re-resolves host/port/data-dir from args, env, settings |
| `server status` | Reports server state | Shows running/not running, resolved URL, WebUI available/unavailable, data directory |

CLI commands are non-interactive — they never open a browser and always print
status to stdout. Exit code 0 = success, 1 = failure.

## Browser / UI Testing

The WebUI is an Agent-first chat surface with a two-pane layout:

- **Left pane** (210px fixed, `--sidebar-width`): Navigation — Chat, Agents, Projects, Cron, System Prompt, Settings, Logs, Statistics, Debug — plus a live connection-status footer ("Connected"/reconnecting).
- **Right pane**: Content area for the selected section

### What to test

| Area | What to check |
|---|---|
| **App shell** | Two-pane layout renders; all nine navigation items are visible and work; switching views preserves state; footer shows live connection status |
| **Chat** | Message list renders; agent-selector chips switch the active agent; message input works; queue behavior (queued messages visible, removable before send); streaming display (reasoning, text, tool rows); token badge; scroll behavior (only timeline scrolls) |
| **Agents** | Agent list loads; create/edit forms work; model/fallback-model selects show correct labels; thinking-effort and temperature; tool & skill toggles; delete rejects when only one agent remains |
| **Projects** | Project list loads; add/refresh; detail shows display name, default agent/model/temperature/thinking, auto-load files, and the tool & skill whitelists (the project ceiling); team is scanned live from the repo |
| **Cron** | Job list or the "No scheduled jobs" empty state; create/edit a scheduled job; completed jobs are hidden from the list |
| **System Prompt** | Fragment editor renders: per-fragment cards (system.md, runtime.md, tools.md, …) with insertable variable chips, a prompt-scope selector (Default / per-agent), per-fragment Reset, and "modified" badges |
| **Settings** | Thirteen sections render: General, Defaults, Skills, Sub-Agents, Compaction, Recall, Web Search, Debug, Specialized Models, Providers, Channels, Extensions, Appearance. Providers lists each connection's accounts with credential status plus Add provider and Update Model DB; Appearance has language and chat-width with a Save button |
| **Logs** | Daily log-file picker, level filter, order toggle, search box, live append; each entry shows timestamp / level / logger / message with a copy action |
| **Statistics** | On-demand report (nothing extra stored) with tabs Overview / Usage / Runs & errors / Tools / Limits; counts and charts render; Refresh re-aggregates |
| **Debug** | Trace list (provider, model, method, status, duration) with expandable rows, a trace-limit control, Clear, and a model probe; the local-storage/redaction notice is shown |
| **Empty states** | No agents, no messages, no sessions, no cron jobs — each renders a sensible empty state |
| **Error states** | Server unreachable, invalid responses, failed RPCs — user sees meaningful feedback |

### Interaction patterns

- Agents are selected, not Sessions — Chat shows the selected Agent's current session
- "New Session" is blocked while an active Run is in progress
- Switching to another Agent during an active Run is allowed
- All user-visible text goes through i18n — no hardcoded strings

### Browser strategy

- Activate the `playwright-cli` skill for all browser interactions
- Use `playwright-cli snapshot` to read the page state before interacting
- Elements are referenced by `e` refs from the snapshot (e.g., `e15`), CSS selectors, or Playwright locators
- After actions that trigger SSE/WebSocket updates (sending a message, creating an agent), take a snapshot to verify the UI reflects the change
- For every browser-visible pass/fail claim, capture and report at least one screenshot path. Screenshots are required evidence for visual UI checks; do not rely only on DOM snapshots or text assertions.
- When testing that content should be hidden from the normal UI, capture a screenshot of the relevant view and explicitly report whether the hidden content is absent.
- For streaming tests: send a message, then take multiple snapshots to observe progressive output (requires provider with API credentials)

## What Can Be Tested Without API Credentials

These work with a fresh data directory and no `.env` file:

- Server health and CLI lifecycle commands (start, stop, restart, status)
- Agent CRUD (create, list, update, delete)
- Session creation
- Settings retrieval
- Connection list (providers shown, all marked `usable: false`)
- Model list (empty — no provider has usable credentials)
- Tool list
- WebUI navigation, layout, empty states, error display
- Agent form validation and edge cases

## What Requires API Credentials

- Sending messages (chat.send, chat.stream) — needs a provider with a valid API key
- Streaming display in the browser (reasoning, text output, tool calls)
- Model selection with real provider data
- Model catalog refresh (model.refresh_db)

If no provider credentials are configured, report what was tested without
credentials and note which features could not be verified.
