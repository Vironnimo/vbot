# CLI

Local command-line accessor for server lifecycle and RPC-backed management
areas. It owns user-visible server lifecycle commands and their
targeting/status contract, but it does not own server business logic.

## Overview

`cli/` is the local process-management and management-command entrypoint used by
both human users and agents. It owns `server start`, `server stop`, `server
restart`, `server status`, and RPC-backed management commands for channels,
providers, models, skills, and config. The CLI is non-interactive and
automation-safe: it never opens the browser and instead prints the resolved
server URL and status information. It manages local vBot server reachability
around the existing `server/main.py` foreground entrypoint, and every CLI
command outside the `server` lifecycle area calls the running server's RPC
contract rather than reading or mutating files directly.

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
- `python cli/main.py channel add --id --platform telegram --agent --token-env [--dm-scope] [--allow ...]`
  - calls `channel.create` over server RPC and creates a persisted channel config
- `python cli/main.py channel list`
  - calls `channel.list` over server RPC and prints deterministic output
- `python cli/main.py channel remove --id`
  - calls `channel.delete` over server RPC
- `python cli/main.py channel enable --id`
  - calls `channel.enable` over server RPC
- `python cli/main.py channel disable --id`
  - calls `channel.disable` over server RPC
- `python cli/main.py channel status --id`
  - calls `channel.status` over server RPC
- `python cli/main.py provider list`
  - calls `connection.list` over server RPC and prints configured connections
- `python cli/main.py model list`
  - calls `model.list` over server RPC and prints available models
- `python cli/main.py model refresh [--provider <id>]`
  - calls `model.refresh_db` over server RPC and refreshes provider model catalogs
- `python cli/main.py skill list`
  - calls `skill.list` over server RPC and prints valid plus invalid skill diagnostics
- `python cli/main.py config`
  - calls `settings.get_raw` over server RPC and prints raw `settings.json`
- `python cli/main.py config get <key>`
  - calls `settings.get_raw` over server RPC and prints one raw top-level settings key
- `python cli/main.py config set <key> <value>`
  - coerces the CLI value to JSON-native data, then calls `settings.set_key` over server RPC

## Conventions

- `server start` is data-dir-scoped for instance selection.
- Every CLI command except `server start`, `server stop`, `server restart`, and
  `server status` requires a reachable vBot server because the CLI is an
  accessor and those areas are RPC-backed, not local file mutations.
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
- Phase 5 does not require separate stale PID or launch-metadata recovery rules;
  live reachability and `/health` detection are the authority.
- CLI-managed background server startup must not bypass the managed
  application logger.
- Channel command output is deterministic and automation-safe. RPC failures and
  malformed envelopes surface as non-zero exits with clear messages.
