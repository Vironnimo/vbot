# Runtime

Bootstrap entry point. Wires services and manages start/stop lifecycle.

## Interfaces

`core/runtime/interfaces.py` — `typing.Protocol` contracts for DI.

- `ConfigProtocol` — `get(key: str, default: Any = None) -> Any`
- `LoggerProtocol` — `debug(msg)`, `info(msg)`, `warning(msg)`, `error(msg)`

## Runtime class

`core/runtime/runtime.py` — constructor injection via `ConfigProtocol`.

Runtime-owned logging is initialized through `LogManager` before normal
bootstrap logging begins.

```
Runtime(config) → config.get("LOG_LEVEL", "INFO") → LogManager
```

- `start()` — idempotent. Resolves the runtime data dir early, initializes
  `LogManager`, and creates the `vbot.core` logger before normal bootstrap
  logging. Then it loads provider/model registries, prepares data directories,
  reads `<data_dir>/.env` as a fallback credential snapshot without mutating
  `os.environ`, instantiates the OAuth `TokenStore`, instantiates the central
  provider credential resolver (process environment takes precedence over the
  data-dir fallback for environment credentials), copies prompt
  fragments, wires services, starts the `ProcessManager` sweeper when startup
  happens inside a running asyncio loop, registers built-in tools, creates the
  shared `ChatRunManager`, creates the non-streaming automation `ChatLoop`, wires
  `TriggerService`, wires and starts `ChannelService` when an event loop is active,
  registers the `channel_send` tool when at least one channel is active, wires
  and starts `CronService` when an event loop is active, registers the `cron`
  tool, creates the in-memory `SubAgentBatchTracker`, registers
  sub-agent tools, and ensures a usable default Agent exists. Writes `Runtime
  started` at info level. Second call is no-op (debug log) and preserves service
  instances.
- `stop()` — writes "Runtime stopped" at info level if logger exists. Stops the
  `ProcessManager` sweeper, stops `ChannelService`, stops `CronService`, resets
  started state, and clears service references.
  Safe to call before `start()`.
- `logger` — public attribute, `LoggerProtocol | None`. Set by `start()`.
- `providers` / `models` — provider and model registries.
- `provider_credentials` — central provider credential resolver; also exposed
  through `has_provider_credentials(provider_id)` and
  `get_provider_credentials(provider_id)`.
- `token_store` — OAuth token persistence service rooted at `<data_dir>/oauth/`.
- `storage` — `StorageManager` for data-dir/settings/prompt fragments.
- `agents` — `AgentStore` for agent CRUD/workspaces.
- `tools` — runtime `ToolRegistry` with built-in tools registered at startup; includes normal tools (`bash`, `edit`, `glob`, `grep`, `process`, `read`, `subagent`, `subagent_result`, `write`) plus the internal `skill` tool when skills are loaded.
- `process_manager` — shared in-memory `ProcessManager` service used by `bash`
  and `process` tools. It owns process sessions, output buffers, TTL sweeping,
  and Run-scoped cancellation.
- `skills` — `SkillRegistry` loaded from `<data_dir>/skills`, bundled `resources/skills`, and configured extra `skill_directories`.
- `chat_sessions` — `ChatSessionManager` rooted at runtime data dir.
- `chat_run_manager` — shared `ChatRunManager` used by server chat flows and
  automation triggers. Runtime also exposes it as `runtime.chat_runs` for server
  compatibility.
- `trigger_service` — `TriggerService` for programmatic Run starts and
  in-memory busy-Session queueing.
- `channel_service` — `ChannelService` for persisted channel configs, adapter
  lifecycle, and outbound platform delivery rooted at `<data_dir>/channels/`.
- `cron_service` — `CronService` for persisted cron and one-shot scheduled
  triggers rooted at `<data_dir>/cron/jobs.json`.
- `system_prompts` — `SystemPromptManager` using runtime storage/tools/skills.
- `reload_skills()` — reloads the skill registry from current settings, re-registers the internal `skill` tool handler, and updates `SystemPromptManager` so prompt catalogs and provider tool visibility use the new registry without restarting the app.
- `reload_channel_tool()` — unregisters `channel_send` and re-registers it when
  `channel_service.has_active_channels()` is true so runtime channel
  enable/disable changes keep tool visibility in sync.
- `core/runtime/__init__.py` exports the runtime class and DI protocol types for callers.

All service properties raise `RuntimeError` before `start()` and after `stop()`.
`stop()` clears the token-store reference along with other runtime services.

`get_adapter(provider_id, connection_id)` builds an async provider-token getter
for the selected connection and instantiates the class selected by
`ProviderConfig.adapter`. Current adapter keys include `openai_compatible`,
`openrouter`, `github_copilot`, and `anthropic`. `api_key` connections resolve
the static credential through `provider_credentials` and receive
`StaticTokenGetter`; OAuth connections with `OAuthConfig` receive
`OAuthTokenGetter` using `runtime.token_store`. OAuth stubs that still have a
credential key but no OAuth metadata remain static credential connections until
configured otherwise.

## Constraints & Gotchas

- On first start with an empty data directory, Runtime creates the bootstrap
  Agent `main` / `Main`. Agent creation also creates the first empty Session and
  persists it as `current_session_id`.
- Existing data directories with at least one Agent are preserved; Runtime does
  not add another `main` Agent just because `main` is absent.
- `ChannelService.start()` follows the same event-loop guard pattern as
  `CronService.start()`: when runtime startup happens without an active asyncio
  loop, the service is wired but listeners are not started.
