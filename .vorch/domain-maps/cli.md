# CLI

Local command-line accessor for server lifecycle and RPC-backed management areas. It owns user-visible command parsing, target resolution, lifecycle process control, and deterministic agent-facing output; it does not own server business logic.

## Overview

`cli/` is the local process-management and management-command entrypoint used by human users and agents. It owns `server start`, `server stop`, `server restart`, `server status`, the local `home` path report, the local `desktop` GUI launch, the local `update` self-updater, the local `uninstall`/data-reset flow, the local `autostart` manager, local `doctor` validation, and RPC-backed management commands for agents, projects, sessions, channels, providers, models, task-model bindings, skills, tools, prompts, logs, cron jobs, Bootstrap jobs, debug traces, statistics, and config. The CLI is automation-safe: it never opens the browser, prints explicit status/output, and exits non-zero for failed management operations except for the documented `server status` non-vBot conflict case. `uninstall` is the sole interactive exception when stdin is a TTY; non-interactive callers must select an explicit removal mode and confirmation flag. Every command outside server lifecycle, home/desktop, update/uninstall/autostart, and doctor calls the running server's RPC contract rather than reading or mutating runtime files directly.

## Interfaces

- `cli/parser.py` owns the `argparse` tree, shared target arguments, choice constants, and all help text; every command description carries an inline `Example:` line. `cli/main.py` owns dispatch functions, exit-code mapping, and central output printers.
- **Primary identifiers are positional arguments, not flags:** `agent show <agent-id>`, `agent create <agent-id> <name>`, `channel remove <channel-id>`, `provider status <provider-id>`, `model refresh [<provider-id>]`, `session list <agent-id>`, `cron delete <job-id>`, `debug trace <trace-id>` - secondary parameters stay flags.
- `home [--data-dir]` is local and read-only (no server needed) and prints exactly two deterministic fields: `vbot_root` is the checkout/install root containing the running vBot code, `data_dir` follows the explicit argument -> `VBOT_DATA_DIR` -> applicable `.vbot-worktree` -> `~/.vbot` chain (`Config` owns selection). Neither field is the caller's arbitrary shell cwd or a Project cwd.
- `cli/rpc_client.py` posts `{ method, params }` to `/api/rpc`, normalizes success/error envelopes into `RpcPayload`, preserves server RPC error code/message text when present, and reports malformed transport responses as command failures. Calls use a 10s timeout by default; methods in `_LONG_RUNNING_METHODS` (currently `model.refresh_db`) use an unbounded read with a short connect timeout, so the server bounds the work while an unreachable server still fails fast.
- The `*_management.py` modules per area translate parsed args into RPC params, reject local no-op mutations when useful, validate only CLI-local input shape, and format deterministic plain-text output.

**Server lifecycle**

- `server start [--host] [--port] [--data-dir]` resolves the target instance and starts `python -m server.main` in the background when no vBot server is already reachable there; it succeeds only after the exact vBot `/health` contract responds. On Windows the child uses a new process group, `CREATE_NO_WINDOW`, and explicit breakaway from any invoking Job, so the server outlives its invoking PowerShell/Cmd without leaving a Python console window open.
- `server stop [--host] [--port] [--data-dir]` refuses non-vBot listeners. When the listener PID matches the current local control record, it posts the per-process secret to the private shutdown route and waits for FastAPI/Runtime teardown; a missing/rejected record falls back to platform termination. Either path force-kills after the bounded timeout.
- `server restart [--host] [--port] [--data-dir] [--service-name]` restarts through the systemd **user** unit when one actively manages exactly the resolved host/port/data-dir (`systemctl --user restart`; shared with `update`; a same-named unit for another instance is never treated as the owner). Otherwise it stops the resolved target if present and re-resolves host/port/data-dir before starting - if stop fails, restart does not start. Service-name validation happens before any path construction or `systemctl` call.
- `server status [--host] [--port] [--data-dir]` reports running/not running, resolved URL, WebUI availability, data dir, and log path. A non-vBot listener prints a conflict note but still exits 0 for `status`; the same conflict fails `start`, `stop`, and `restart`.
- Every lifecycle invocation flushes an action-and-target sentence before work and ends with a complete outcome sentence; no-op states, conflicts, forced termination, and failures are stated explicitly.

**Local accessor actions**

- `desktop [--host] [--port]` opens the pywebview Desktop window pointed at a local or remote server. It is neither an RPC command nor server lifecycle: it branches before the shared resolver, lazily imports `desktop.main` (the default CLI path needs no pywebview), forwards only the flags actually supplied - bare `vbot desktop` reaches the launcher's last-used auto-connect path instead of a silent localhost target - takes no `--data-dir` (Desktop has its own per-user config dir; see `desktop.md`), and blocks until the window closes, then prints `desktop window closed`.
- Package installation separately exposes `vbot-desktop` as a GUI-script entrypoint for windowless Start-menu launch; the CLI `desktop` remains the console launch surface.

**Bootstrap**

- `bootstrap list|create|update|delete|enable|disable` maps to the matching RPCs and is the only Bootstrap management surface. Create requires `--mode once|always` plus either a positional Agent address (optional `--session`) or `--current-session`, which reads `VBOT_RUN_AGENT_ID`, `VBOT_RUN_SESSION_ID`, and optional `VBOT_RUN_PROJECT_ID` as injected by the Bash Tool from its `ToolContext`; conflicting explicit targets, missing Run context, and remote server targets are rejected. Update supports `--clear-session`; completed one-shots remain immutable server-owned history.

**Agent area**

- Primary ids positional: `agent create <id> <name>`, `rename <old-id> <new-id>`. `reorder <id>...` reads the current listing's `order_revision`, appends unlisted agents in their relative order, rejects duplicate ids locally, and prints roster order plus revision.
- Create/update accept the shared Run, Memory, prompt, Skill, delegation (`--subagent-allow`), Agent Policy (`--compaction-policy`), and explicit Tool Access Policy flags (`--tool-access-mode all|selected|none`, repeatable `--tool-allow`/`--tool-deny`); wildcard and empty-array conventions are not part of this surface. Update additionally exposes clear-model/Policy flags, Workspace relocation (`--workspace` / `--default-workspace`), Identity-Agent Project selection (`--project` / `--clear-project`), name, and current Session.
- Rename is its own RPC because the id change coordinates the complete Identity Agent tree and live references; never fold it into ordinary update flags. Location flags preserve the core separation: Project controls cwd for relative file/shell work, Workspace stays the SOUL/Memory home.
- `--copy-workspace-files` requires a Workspace destination in the same command. Clear/default flags send JSON `null` (or the stored empty-model sentinel); literal domain values such as `--thinking-effort none` remain distinct from clearing the field.
- A full Agent response without effective Model persists successfully (onboarding resource) but appends an explicit cannot-run warning plus recovery commands (`model list --task chat`, `agent update --model`).

**Channel area**

- Add requires exactly one token source: recommended `--token-stdin` sends the UTF-8 value as a write-only managed token; `--token-env` references an externally managed variable name. `set-token <id> --stdin` rotates the credential and prints non-secret source/applied state, targeted-adapter restart decision, immediate health, and a verification command. Tokens are never command-line arguments or output (see Gotchas).
- `--platform` accepts `telegram` or `discord`; `--response-mode`/`--observe-unaddressed` expose the Channels-owned group-gating policy. `channel identity/access/grant-admin/revoke-admin` manage the Channel account identity and saved group roles (additive, idempotent mutations). `channel status` prints enabled/running/failed plus failure reason and denied inbound chats with an allowlist suggestion. Group-policy semantics live in `channels.md`.

**Provider area**

- `provider list/status` read `connection.list` (rows include enabled/usable/reachable plus account sub-lines). `provider usage` prints live subscription plans; `provider usage-history [-clear --yes]` reads Provider-owned durable observations - separate from Session statistics.
- `custom-save` is a complete replacement of the Custom Provider record; repeated `--model` values create conservative manual chat Models, and the optional key is write-only - output never echoes it.
- `enable|disable` is the connection switch: keyless local providers start disabled, multi-connection providers require `--connection` (candidates listed on failure), enable probes local auto-refresh reachability and hints `provider set-key` when no credential exists.
- `set-key <id> <api-key> [--connection] [--account] [--refresh-models]` writes the resolved account's derived credential to the data-dir `.env`, reloads runtime credentials live, rejects OAuth connections, and never returns or prints the secret; `--refresh-models` chains a provider-scoped Model DB refresh after success. `unset-key` reports when an account is still configured via the process environment.
- `connect` starts the OAuth device flow, printing `user_code`, `verification_uri`, expiry, and the follow-up `connect-status` command; the server polls in the background, so the CLI stays non-interactive. All credential commands accept `[--account <account-id>]` and render the resolved account - semantics in `providers/connections.md`.

**Model area**

- `model list` exposes Provider/capability/task/modality/minimum-context filters; the server includes only Models with at least one usable Connection. `model list --task chat` is the primary recovery/discovery command for an Agent without an effective Model. `model show` prints the complete loaded projection as deterministic JSON.
- `model refresh [<provider-id>]` stages a complete copy of the active Model DB, refreshes requested Provider projections, atomically publishes under `<data_dir>/artifacts/models/`, and never writes the installed checkout. `scripts/refresh_model_db.py` is the maintainer-only client for the tracked `resources/models/` root; the RPC rejects cross-checkout targets. Snapshot mechanics live in `models.md`.

**Project area**

- `project add/list/show/set/set-override/clear-override/detect/rm` mirror their RPCs. `detect [<path>]` reports per-format agent/skill presence plus context-file facts and answers a nonexistent path explicitly. Override mutations target one Team Agent field without editing the repo; a Tool override replaces the scanned repository Tool policy inside the Project Tool Whitelist (resolution tiers: `projects/resolution.md`). `rm --copy-rooted-agent-files` archives the anchor and copies rooted agents' identity files, reporting archive path and affected agents. Mutations print the saved Project plus fresh scan preview with provenance.
- Project work is stateless - there is no current-Project flag; Projects ride the address form below.

- **`agent@projekt` address form on positional agent arguments:** the `session` and `cron` positionals accept `agent@projekt` and pass the string straight to the RPC, which owns the one parse seam (`core.projects.parse_agent_address`); the CLI does not re-parse. A bare name is identity. Project agents display in address form. `session link-channel` keeps the bare `<agent-id>` form (channels stay identity-only).

**Session & Cron areas**

- `session fork` optionally re-homes the copy via `--target-agent`; `set-compaction-policy` sets a full JSON Session Policy or clears it to resume inheritance and prints override/effective/source; `delete` refuses without `--yes` and prints the landing `next_session_id`; `link-channel` links a Channel to an identity-owned session.
- `cron create <agent-id> --name <name> --prompt <text>` requires a human-readable name and exactly one of `--cron <expression>` (recurring) or `--at <iso-datetime>` (one-time; derives `schedule_type`). Cron expressions and offset-free Once values use the server timezone; the CLI has no timezone flag. `update` accepts the same flags plus `--status active|paused` and rejects empty updates before RPC.

**Diagnostics areas**

- `debug trace <trace-id>` prints the full sanitized trace as sorted JSON; `probe` prints status/duration/trace id plus model preview and never dumps raw responses (points at `trace`). Listing/probing fail server-side while Debug Mode is disabled; `status`/`clear` always work.
- `task-model` validates `<task-type>` locally against `SUPPORTED_TASK_TYPES`; `options` prints the schema plus configured/effective values; `set-option --stdin` carries JSON values without shell-quoting loss; every successful mutation prints the saved binding; `clear` sends an empty target.
- `skill` writes only `global` / `agent:<id>` scopes (Project/bundled/Extension unwritable), takes content from a local `--file`, and requires `--yes` for destructive operations; `inventory` reports every scanned package with origin, owner, status, warnings, and shared-with receivers.
- `memory <agent-id> list|add|replace|remove` selects `--scope agent|user` (default agent), takes text from `--content` or `--file`, needs `--yes` for remove; `list` prints entry ids per scope so follow-ups can address entries. `tool list` prints registered public Tools.
- `statistics <section>` makes one `statistics.report` call and renders only the requested section with explicit empty states; `--since/--until` pass through verbatim (server validates ISO-8601 and window order).

**Extensions area**

- Name-first routing: the first token is either a reserved verb (`list`/`reload`/`enable`/`disable`) or an extension name, dispatched through a `selector` + `rest` positional pair (argparse cannot enumerate dynamic names). `reload` performs the full restart-equivalent rebuild and prints count summary plus per-failure lines and a follow-up pointer when anything failed.
- `enable|disable <name>` read the current `extensions` settings section and write the complete section back through `settings.update`; both apply live without restart. Enabling re-lists and warns when the freshly rebuilt extension did not load.
- `<name>` shows one extension's settings: each schema field with type + current value, secrets only as `set`/`not set` (schema-less falls back to raw config). `<name> set <field> <value>` routes by declared schema type: a `secret` field calls `extensions.set_secret` (caller names the schema field key; server maps it to the declared `.env` key), anything else is coerced to its declared type and written to live config. `--stdin` reads the value out of shell history; an empty value clears a secret. Unknown extension/field get candidate + did-you-mean output.

**Prompt / Log / Config / Doctor**

- `prompt` commands accept `--scope default|agent:<id>`; the default object is omitted toward the RPC because the server defines it. Layout is locally shape-checked JSON; preview prints text plus Tool-definition token metadata.
- `log list|read` map to their RPCs - the CLI must never read `<data_dir>/logs/` directly. `log read <daily-log-name>` prints parsed entries plus the returned cursor.
- `config` is the public Settings-path catalog: `describe/get/set/unset/patch` map to `settings.catalog/get_path/patch`. Patch operations form one atomic mutation (invalid/overlapping persist nothing); values are JSON-decoded before string fallback; dynamic keys are bracketed JSON strings. Mutation output reports active value, pending restart value, application lifecycle, and aggregate `restart_required`. `config effective` prints `settings.values`; `config raw` is the diagnostic-only internal document. Secrets stay on their dedicated commands.
- `doctor settings|config [--data-dir]` run locally without a reachable server: strict `settings.json` validation, respectively whole-bundle discovery delegating each file to its owning domain. Missing files report OK when defaults apply; diagnostics render as severity, JSON path, message.

## Conventions

- Shared `--host`, `--port`, and `--data-dir` target options apply to server lifecycle, uninstall/reset, and RPC-backed management commands. Doctor commands accept only `--data-dir` because they are local file validators.
- Port resolution follows `--port` > `VBOT_SERVER_PORT` > validated `settings.json` port keys > `8420`, using the shared resolver in `core/utils/config.py`; the CLI imports it from there and does **not** import the `server` package. Ambient `PORT`/`SERVER_PORT` process variables are ignored - those names matter only as settings keys inside `settings.json`.
- All local CLI HTTP disables proxy/`.netrc` environment trust (`trust_env=False`) across transport, probes, and updater: the CLI talks loopback only and bodies carry secrets, so ambient proxies can never divert credentials off-host. A target counts as vBot only when `/health` answers per the identity contract owned by `server.md`.
- Built WebUI assets are optional: missing `webui/dist` leaves `/health` healthy while `/` may be unavailable; lifecycle output reports `webui: unavailable` instead of treating startup as failed.
- CLI output is agent-facing. Success and failure must be explicit enough for an agent to choose the next command without guessing; silent success and silent failure are invalid, and central printers emit fallback lines if a result unexpectedly carries none. Mutations name target and action; reads print structured state for follow-ups. Failures include the server RPC code/message; for bounded identifiers prefer candidate suggestions ("did you mean").
- The process entrypoint reconfigures stdout/stderr to UTF-8 with `backslashreplace`, so Unicode-rich payloads print safely on legacy consoles. Explicit stdin readers (extension secrets, channel tokens) accept a leading BOM, strip trailing CR/LF, and return normal failure on decode errors rather than falling back to a legacy code page that could silently corrupt a secret.
- Local lifecycle subprocesses capture bytes rather than locale-decoded text; `decode_command_output()` prefers UTF-8 and falls back to the process locale with escaped undecodable bytes, so decode failures cannot erase output used for track or status decisions.
- Lifecycle logs belong under `<data_dir>/logs/` through the managed `LogManager`; background server startup must not bypass that logger or rely on raw child streams.
- Tests mirror the module split under `tests/cli/`; parser shape, behavior, output text, and lifecycle changes update the focused tests.
- The bundled product skill `resources/skills/vbot-cli/` teaches vBot's own Agents the CLI surface (lean SKILL.md routing to per-area reference files). Its update workflow creates and verifies a one-shot Bootstrap around `vbot update`, then inspects server status/logs from that Run. When command shapes, areas, or flags change, update the matching reference file (and SKILL.md's area table if areas/subcommands changed) in the same change.

## Constraints & Gotchas

- The CLI is an accessor, not a second control plane. Do not add management commands that write `settings.json`, Agent configs, Channel configs, prompt fragments, logs, model catalogs, or other runtime files directly; route them through server RPC unless the command is explicitly local lifecycle/home/desktop-launch/doctor/update/uninstall/autostart behavior. `home` only reports locally resolved paths; the `desktop` launch is a local GUI action. Neither uses RPC or constructs a `ServerInstance`.
- If a vBot server already runs at the target, `start` reports `already running` and spawns nothing. A reachable non-vBot occupant conflicts: `start`/`stop`/`restart` fail and must not terminate that process, while `status` reports it and exits 0.
- Process termination is allowed only after `/health` confirms the target is vBot. Local process lookup matches the resolved host/address and port, not port alone; wildcard listeners match only when they can receive the requested traffic.
- Stop is best effort: terminate first, kill after the bounded timeout, report `forced: true` when the kill fallback ran. If the bounded post-kill wait also times out, `stop_server` returns a failed `CommandResult` rather than raising or claiming success. On Windows this can still interrupt in-flight Runs abruptly.
- Live reachability and `/health` classification are the authority - the CLI intentionally has no stale-PID or launch-metadata recovery path.
- Startup waits for health readiness and cleans up the just-spawned child on readiness timeout, early child exit, or a non-vBot responder appearing during startup.
- `provider set-key` accepts a direct API-key value because agents configure local instances through the CLI; the value goes only to `provider.set_key` and never into success/error/log/refresh output.
- Channel token values must never be arguments or output. Managed setup/rotation reads them only from stdin and sends them only to `channel.create`/`channel.set_token`; `--token-env` carries a variable name, never a token.

## References

Read these only when your task matches - not by default.

- Changing installer, updater, uninstaller, autostart internals, install shapes, or `.vbot-install.json` handling -> `cli/lifecycle-management.md`
