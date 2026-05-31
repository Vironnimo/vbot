# CLI

Local command-line accessor for server lifecycle and RPC-backed management
areas. It owns user-visible server lifecycle commands and their
targeting/status contract, but it does not own server business logic.

## Overview

`cli/` is the local process-management and management-command entrypoint used by
both human users and agents. It owns `server start`, `server stop`, `server
restart`, `server status`, and RPC-backed management commands for agents,
channels, providers, models, skills, tools, prompts, logs, and config. The CLI is non-interactive and
automation-safe: it never opens the browser and instead prints the resolved
server URL and status information. It manages local vBot server reachability
around the existing `server/main.py` foreground entrypoint, and every CLI
command outside the `server` lifecycle area calls the running server's RPC
contract rather than reading or mutating files directly.
Shared RPC transport, envelope parsing, timeout handling, and RPC error message
formatting live in `cli/rpc_client.py`; management modules own only command
parameter translation and deterministic output formatting.

## Interfaces

- `python cli/main.py server start [--host] [--port] [--data-dir]`
  - resolves the target instance configuration
  - starts the server if no vBot server is already reachable at the target
  - succeeds only when `GET /health` responds successfully
- `python cli/main.py server stop [--host] [--port] [--data-dir]`
  - targets an already-running local vBot server at the resolved address
  - attempts graceful shutdown, then force-stop after a bounded timeout if needed
- `python cli/main.py server restart [--host] [--port] [--data-dir]`
  - stops the target local vBot server if present
  - re-resolves host/port/data-dir from current args, env, and settings before restart
- `python cli/main.py server status [--host] [--port] [--data-dir]`
  - reports at least: running/not running, resolved URL, WebUI available/unavailable, and resolved `data_dir`
- `python cli/main.py agent list`
  - calls `agent.list` over server RPC and prints deterministic agent rows
- `python cli/main.py agent show --id`
  - calls `agent.get` over server RPC and prints one agent with mutable fields, allowlists, workspace, current session, and context window
- `python cli/main.py agent create --id --name [--model] [--fallback-model] [--temperature] [--thinking-effort] [--allowed-tools ...] [--allowed-skills ...]`
  - calls `agent.create` over server RPC and creates a persisted agent config
- `python cli/main.py agent update --id [--name] [--model] [--fallback-model] [--temperature] [--clear-temperature] [--thinking-effort] [--clear-thinking-effort] [--allowed-tools ...] [--allowed-skills ...] [--current-session-id]`
  - calls `agent.update` over server RPC; clear flags send JSON `null` for inherited defaults, while `--thinking-effort none` sends the literal `"none"` effort
  - rejects empty updates before RPC and prints the valid update flags
- `python cli/main.py agent delete --id`
  - calls `agent.delete` over server RPC and relies on server-side last-agent, busy-agent, and reference checks
- `python cli/main.py channel add --id --platform telegram --agent --token-env [--dm-scope] [--allow ...]`
  - calls `channel.create` over server RPC and creates a persisted channel config
- `python cli/main.py channel list`
  - calls `channel.list` over server RPC and prints deterministic output
- `python cli/main.py channel remove --id`
  - calls `channel.delete` over server RPC
- `python cli/main.py channel update --id [--platform telegram] [--agent] [--token-env] [--dm-scope] [--allow ...] [--enabled true|false]`
  - calls `channel.update` over server RPC; omitted fields remain unchanged, and `--allow` replaces the full allowed chat-id list
  - rejects empty updates before RPC and prints the valid update flags
- `python cli/main.py channel enable --id`
  - calls `channel.enable` over server RPC
- `python cli/main.py channel disable --id`
  - calls `channel.disable` over server RPC
- `python cli/main.py channel status --id`
  - calls `channel.status` over server RPC
- `python cli/main.py provider list`
  - calls `connection.list` over server RPC and prints configured connections
- `python cli/main.py provider status --provider <id> [--connection <provider:connection-id>]`
  - calls `connection.list` over server RPC and prints only the matching provider or connection rows
- `python cli/main.py provider set-key --provider <id> [--connection <provider:connection-id>] --value <api-key> [--refresh-models]`
  - calls `provider.set_key` over server RPC, writes the API-key connection's configured credential key to the data-dir `.env`, reloads runtime provider credentials, and never echoes the API key in output
  - when `--refresh-models` is present, also calls `model.refresh_db` for the same provider and reports refresh outcome after the set-key line
- `python cli/main.py model list`
  - calls `model.list` over server RPC and prints available models
- `python cli/main.py model refresh [--provider <id>]`
  - calls `model.refresh_db` over server RPC and refreshes provider model catalogs
- `python cli/main.py skill list`
  - calls `skill.list` over server RPC and prints valid, unavailable, optional-missing, and invalid skill diagnostics
- `python cli/main.py tool list`
  - calls `tool.list` over server RPC and prints registered public tools
- `python cli/main.py prompt list`
  - calls `prompt.list` over server RPC and prints editable prompt fragments with modified state and variable placeholders
- `python cli/main.py prompt update --name <fragment> (--content <text>|--file <path>)`
  - reads direct content or one local source file, then calls `prompt.update` over server RPC
- `python cli/main.py prompt reset --name <fragment>`
  - calls `prompt.reset` over server RPC
- `python cli/main.py prompt preview --agent <agent-id>`
  - calls `prompt.preview` over server RPC and prints token metadata plus rendered System Prompt text
- `python cli/main.py log list`
  - calls `log.list` over server RPC and prints available daily log files and the default newest file
- `python cli/main.py log read --file <daily-log-name>`
  - calls `log.read` over server RPC and prints parsed log entries plus the returned cursor
- `python cli/main.py config`
  - calls `settings.get_raw` over server RPC and prints raw `settings.json`
- `python cli/main.py config get <key>`
  - calls `settings.get_raw` over server RPC and prints one raw top-level settings key
  - when the key is missing, prints available top-level keys and a `did you
    mean` suggestion when a close match exists
- `python cli/main.py config set <key> <value>`
  - coerces the CLI value to JSON-native data, then calls `settings.set_key` over server RPC
- `python cli/main.py doctor settings [--data-dir <path>]`
  - runs locally without requiring a reachable server
  - validates the target data-dir `settings.json` with the central JSON validator
  - reports `ok` for missing files because defaults will be used
  - reports diagnostics as `severity`, JSON path, and message for agent callers
- `python cli/main.py doctor config [--data-dir <path>]`
  - runs locally without requiring a reachable server
  - validates the target data-dir user-editable JSON config bundle:
    `settings.json`, `agents/*/agent.json`, `channels/*/channel.json`, and
    `cron/jobs.json` when present
  - prints explicit file counts, error/warning counts, and per-file diagnostics
    for agent callers

## Conventions

- `server start` is data-dir-scoped for instance selection.
- The CLI is primarily agent-operated. Every command and subcommand must have
  useful `--help` text, including purpose and important arguments. Success and
  failure output must be explicit; silent success or silent failure is invalid.
  Central output printers must emit a fallback success/error line if a command
  result unexpectedly contains an empty message.
- Successful mutating commands must name the target and the action performed.
  Read commands must print enough structured state for an agent to decide the
  next command without guessing.
- Failures must include the server RPC error code/message when available and an
  actionable hint when the CLI has enough local candidates to produce one. For
  bounded identifiers such as providers, connections, settings keys, agents,
  channels, prompt fragments, and log files, prefer `did you mean` suggestions
  over bare not-found output.
- Every CLI command except `server start`, `server stop`, `server restart`,
  `server status`, and local `doctor` commands requires a reachable vBot server
  because the CLI is an accessor and those areas are RPC-backed, not local file
  mutations. `doctor config` is the local preflight for manually edited runtime
  JSON before server/RPC paths consume it.
- Port resolution follows `--port` > `VBOT_SERVER_PORT` > `settings.json` > `8420`.
- Ambient `PORT` and `SERVER_PORT` process environment variables are ignored for
  port resolution; only `VBOT_SERVER_PORT` can override `settings.json` from the
  environment.
- A target counts as vBot only when `/health` matches the vBot health contract.
  The current health contract is HTTP `200` with JSON `{ "status": "ok" }`.
- If a vBot server is already running at the target address/port, `start`
  reports that cleanly instead of launching another instance.
- If a non-vBot process occupies the target address/port, `start`, `stop`, and
  `restart` fail with a clear conflict error and must not terminate that process.
- `status` reports "not running" for vBot in that conflict case and adds a note
  that another service is using the target address/port.
- Logs for the managed instance belong under `<data_dir>/logs/`.
- The CLI never opens a browser.
- Process termination is allowed only after `/health` confirms the target is a
  vBot server, and local process lookup must match the resolved host/address and
  port rather than port alone.

## Constraints & Gotchas

- Built WebUI assets are optional at runtime. If `webui/dist` is missing, the
  API server may still be healthy; CLI output must report WebUI unavailable
  rather than treating startup as failed.
- On Windows, graceful shutdown may fall back to abrupt termination after the
  timeout. In-flight Runs may be interrupted.
- The CLI does not require separate stale PID or launch-metadata recovery rules;
  live reachability and `/health` detection are the authority.
- CLI-managed background server startup must not bypass the managed
  application logger.
- Provider, channel, tool, prompt, and log command output is deterministic and automation-safe. RPC failures and
  malformed envelopes surface as non-zero exits with clear messages.
- Provider `set-key` accepts a direct API-key value because agents are expected
  to configure local vBot instances through the CLI. It must send the value only
  to `provider.set_key` and must not echo the value in success or error output.
- Agent command output is deterministic and automation-safe. Agent create/update
  accepts only public mutable RPC fields; workspace mutation remains server-rejected.
- Prompt updates may read a local source file, but storage writes still happen only through `prompt.update`; log commands must not read `<data_dir>/logs/` directly.
