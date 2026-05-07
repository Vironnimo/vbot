# Runtime

Bootstrap entry point. Wires services and manages start/stop lifecycle.

## Interfaces

`core/runtime/interfaces.py` — `typing.Protocol` contracts for DI.

- `ConfigProtocol` — `get(key: str, default: Any = None) -> Any`
- `LoggerProtocol` — `info(msg)`, `error(msg)`, `debug(msg)`

## Runtime class

`core/runtime/runtime.py` — constructor injection via `ConfigProtocol`.

```
Runtime(config) → config.get("LOG_LEVEL", "INFO") → LogManager
```

- `start()` — idempotent. Creates `vbot.core` logger via `LogManager`, loads provider/model registries, prepares data directories, loads `<data_dir>/.env` into the process environment without overwriting existing env vars, copies prompt fragments, wires services, registers built-in tools, and ensures a usable default Agent exists. Writes "Runtime started" at info level. Second call is no-op (debug log) and preserves service instances.
- `stop()` — writes "Runtime stopped" at info level if logger exists. Resets started state and clears service references. Safe to call before `start()`.
- `logger` — public attribute, `LoggerProtocol | None`. Set by `start()`.
- `providers` / `models` — provider and model registries.
- `storage` — `StorageManager` for data-dir/settings/prompt fragments.
- `agents` — `AgentStore` for agent CRUD/workspaces.
- `tools` — runtime `ToolRegistry` with built-in tools registered at startup; currently includes `read`.
- `skills` — `SkillRegistry` loaded from `<data_dir>/skills`.
- `chat_sessions` — `ChatSessionManager` rooted at runtime data dir.
- `system_prompts` — `SystemPromptManager` using runtime storage/tools/skills.
- `core/runtime/__init__.py` exports the runtime class and DI protocol types for callers.

All service properties raise `RuntimeError` before `start()` and after `stop()`.

## Constraints & Gotchas

- On first start with an empty data directory, Runtime creates the bootstrap
  Agent `main` / `Main`. Agent creation also creates the first empty Session and
  persists it as `current_session_id`.
- Existing data directories with at least one Agent are preserved; Runtime does
  not add another `main` Agent just because `main` is absent.
