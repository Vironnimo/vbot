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
  `os.environ`, instantiates the central provider credential resolver (process
  environment takes precedence over the data-dir fallback), copies prompt
  fragments, wires services, registers built-in tools, and ensures a usable
  default Agent exists. Writes `Runtime started` at info level. Second call is
  no-op (debug log) and preserves service instances.
- `stop()` — writes "Runtime stopped" at info level if logger exists. Resets started state and clears service references. Safe to call before `start()`.
- `logger` — public attribute, `LoggerProtocol | None`. Set by `start()`.
- `providers` / `models` — provider and model registries.
- `provider_credentials` — central provider credential resolver; also exposed
  through `has_provider_credentials(provider_id)` and
  `get_provider_credentials(provider_id)`.
- `storage` — `StorageManager` for data-dir/settings/prompt fragments.
- `agents` — `AgentStore` for agent CRUD/workspaces.
- `tools` — runtime `ToolRegistry` with built-in tools registered at startup; includes normal tools (`edit`, `glob`, `grep`, `read`, `write`) plus the internal `skill` tool when skills are loaded.
- `skills` — `SkillRegistry` loaded from `<data_dir>/skills`, bundled `resources/skills`, and configured extra `skill_directories`.
- `chat_sessions` — `ChatSessionManager` rooted at runtime data dir.
- `system_prompts` — `SystemPromptManager` using runtime storage/tools/skills.
- `reload_skills()` — reloads the skill registry from current settings, re-registers the internal `skill` tool handler, and updates `SystemPromptManager` so prompt catalogs and provider tool visibility use the new registry without restarting the app.
- `core/runtime/__init__.py` exports the runtime class and DI protocol types for callers.

All service properties raise `RuntimeError` before `start()` and after `stop()`.

## Constraints & Gotchas

- On first start with an empty data directory, Runtime creates the bootstrap
  Agent `main` / `Main`. Agent creation also creates the first empty Session and
  persists it as `current_session_id`.
- Existing data directories with at least one Agent are preserved; Runtime does
  not add another `main` Agent just because `main` is absent.
