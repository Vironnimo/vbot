# Live Testing Guide

Project-specific instructions for testing the running vBot application via CLI
and browser. This file is for the Tester agent — not for writing unit or
integration tests (that is the Builder's job with pytest/Vitest).

## Prerequisites

Before starting any live test session:

1. Python virtual environment is active and vBot is installed
2. No other process on the target port (default 8420)

If the environment is not ready, report **Blocked** — do not attempt to install
packages or set up the environment yourself.

## Starting the App

Run the test-env script. It builds the frontend (if needed), starts the server,
and waits until the health check passes:

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

- **Left pane** (210px fixed): Navigation — Chat, Agents, System Prompt, Settings
- **Right pane**: Content area

### What to test

| Area | What to check |
|---|---|
| **App shell** | Two-pane layout renders; navigation items are visible and work; switching views preserves state |
| **Chat** | Message list renders; message input works; queue behavior (queued messages visible, removable before send); streaming display (reasoning, text, tool rows); scroll behavior (only timeline scrolls) |
| **Agents** | Agent list loads; create/edit forms work; model/fallback-model selects show correct labels; tool toggles; delete rejects when only one agent remains |
| **Settings** | Three sub-views render: General (server host, data directory), Providers (credential status, model counts, Update Model DB button), Appearance (language preference, save button). Provider connections shown with credential status. |
| **System Prompt** | Placeholder page renders |
| **Empty states** | No agents, no messages, no sessions — each renders a sensible empty state |
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
