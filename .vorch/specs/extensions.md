# Extensions

In-process Python extension hooks loaded by the runtime. Owns discovery, module loading, and handler registration; `core/chat/` owns all event dispatch and payload interpretation.

## Overview

`core/extensions/` lets power users extend vBot without editing application source. Runtime loads Python modules from `<data_dir>/extensions/` plus optional extra scan roots from `settings.json` `extension_directories`, then passes a `HooksAPI` object into each extension's `register(api)` function. The module only stores handlers and loads modules; it does NOT dispatch events. `core/chat/` drives every hook by iterating the registry's handler list per event and interpreting each event's payload and return value. Extensions run in-process on the normal asyncio event loop when one exists.

## Data Model

- `HookContext` (frozen dataclass) — passed as the first positional argument to every handler.
  - `session_id: str`
  - `agent_id: str`
- `ExtensionRegistry`
  - `_handlers: dict[str, list[tuple[str, Callable]]]` — event name → ordered `(extension_name, handler)` pairs in load order.
  - Event names are a fixed, closed set defined by `core/chat/`: `run_start`, `before_agent_start`, `context`, `tool_call`, `tool_result`, `run_end`. Registering any other name is silently inert — nothing fires it.

## Interfaces

- `HooksAPI(registry, extension_name)` — thin registration facade passed into an extension's `register(api)`.
- `HooksAPI.on(event, handler) -> None` — appends `(extension_name, handler)` to `registry._handlers[event]`. Every handler is called as `handler(ctx, **payload)`.
- `ExtensionRegistry.load(extensions_dir, extra_dirs=None) -> ExtensionRegistry` — scans immediate children of each root in order (`extensions_dir` first), imports them, and runs `register(api)` when present.
- `ExtensionRegistry.fire(event, ctx, **payload) -> list[Any]` — generic dispatch helper. **Currently unused**: chat does its own per-event iteration over `_handlers` and never calls `fire()`. Do not treat it as the event contract.
- Discovery accepts three entry-point shapes per immediate child:
  - single-file module: `<root>/<name>.py`
  - package module: `<root>/<name>/__init__.py`
  - directory fallback: `<root>/<name>/extension.py`
  - package entry points load under the synthetic `vbot_ext` namespace so relative imports inside extension packages work.

## Hook events (chat contract)

Each handler receives `ctx: HookContext` first, then the event kwargs below. Handlers may be sync or async; async results are awaited.

- `run_start(ctx, session_id, agent_id)` — fired before the user turn is appended. Return value ignored.
- `before_agent_start(ctx, agent, session, messages, run)` — fired after request messages are built. May return `{"system_prompt_append": str}`; the string is appended to the system message (`messages[0]`), but only when that message's `content` is a string. All handlers run and appends accumulate.
- `context(ctx, messages)` — fired before each provider request, on a shallow per-message copy. May return a `list` to fully replace the request messages; the first handler that returns a list wins and the rest are skipped.
- `tool_call(ctx, tool_name, tool_call_id, input)` — fired before a tool runs. May return a dict result envelope to short-circuit execution; the first valid envelope wins and the tool is not dispatched. The dict must be a valid, JSON-serializable tool-result envelope or it is ignored.
- `tool_result(ctx, tool_name, tool_call_id, input, result)` — fired after a tool runs. A returned dict is shallow-merged onto the live envelope and re-validated; all handlers run in turn so each can patch. Invalid patches are ignored and the prior result is kept.
- `run_end(ctx, session_id, agent_id, outcome)` — fired in a `finally`, so it always runs. `outcome` is `"success"`, `"error"`, or `"cancelled"`. Return value ignored.

## Conventions

- Extension discovery is shallow: only immediate children of each configured root are considered.
- Load order is deterministic by sorted extension name within each root; roots are processed in configured order.
- `register(api)` may be sync or async. Async registration is scheduled on the current running loop when startup already happens inside one; otherwise it is completed via `asyncio.run()`.
- Failures are fail-open. Load/`register()` failures log at `error`; per-event handler failures log at `warn` and never abort the run.
- `core/extensions/` stays intentionally small: it stores handlers and loads modules only. Event dispatch and all event-specific semantics live in `core/chat/`.
- Hook-returned result dicts must be valid stable result envelopes and JSON-serializable. Invalid override or patch dicts are ignored by chat rather than aborting the run.

## Constraints & Gotchas

- Extension modules run arbitrary in-process Python and therefore share the kernel's trust boundary.
- `core/chat/` reads the private `_handlers` dict directly — its shape (`event -> list[(name, handler)]`) is a load-bearing contract. Keep it stable when refactoring the registry.
- `fire()` is dead code: nothing calls it. Adding a new hook means writing a per-event loop in `core/chat/`, not wiring `fire()`.
- `context` hook isolation is shallow-copy only: top-level message dict mutations are isolated per request, but deep nested mutations can still affect shared objects.
- `tool_call` short-circuits with the first valid envelope; `tool_result` lets every handler shallow-merge-patch the envelope in turn. Both validate against the tool-result envelope schema and silently drop invalid dicts.
- `settings.extension_directories` must be a list of non-empty strings; a non-list value is ignored with a warning, invalid entries are skipped, and paths are `expanduser()`-expanded.
