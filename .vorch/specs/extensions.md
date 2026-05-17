# Extensions

In-process Python extension hooks loaded by the runtime. Owns discovery, module loading, handler registration, and the shared hook registry used by chat/runtime integration points.

## Overview

`core/extensions/` lets power users extend vBot without editing application source. Runtime loads Python modules from `<data_dir>/extensions/` plus optional extra scan roots from `settings.json` `extension_directories`, then passes a `HooksAPI` object into each extension's `register(api)` function. The module owns handler discovery and lifecycle semantics; `core/chat/` owns the event-specific dispatch behavior and payload interpretation. Extensions run in-process on the normal asyncio event loop when one exists.

## Data Model

- `HookContext`
  - `session_id: str`
  - `agent_id: str`
- `ExtensionRegistry`
  - `_handlers: dict[str, list[tuple[str, Callable]]]`
  - Keys are event names such as `run_start`, `run_end`, `before_agent_start`, `context`, `tool_call`, and `tool_result`.
  - Values are ordered `(extension_name, handler)` pairs in extension load order.

## Interfaces

- `HooksAPI(registry, extension_name)` — thin registration facade passed into an extension's `register(api)` function.
- `HooksAPI.on(event, handler) -> None` — appends the handler to `registry._handlers[event]` under the current extension name.
- `ExtensionRegistry.load(extensions_dir, extra_dirs=None) -> ExtensionRegistry` — scans immediate children of each root, imports extensions, and runs `register(api)` when present.
- `ExtensionRegistry.fire(event, ctx, **payload) -> list[Any]` — runs all registered handlers for simple lifecycle events, awaiting async handlers when needed and returning only non-`None` results.
- Discovery accepts three entry-point shapes per immediate child:
  - single-file module: `<root>/<name>.py`
  - package module: `<root>/<name>/__init__.py`
  - directory fallback: `<root>/<name>/extension.py`
  - package entry points are loaded under the synthetic `vbot_ext` namespace so relative imports inside extension packages work.

## Conventions

- Extension discovery is shallow: only immediate children of each configured root are considered.
- Extension load order is deterministic by sorted extension name within each root.
- `register(api)` may be sync or async. Async registration is scheduled on the current running loop when startup already happens inside one; otherwise it is completed via `asyncio.run()`.
- Handler failures are fail-open. Loading/register failures log at `error`; per-event handler failures log at `warn` and do not abort the run.
- `core/extensions/` stays intentionally small: it stores handlers and performs generic dispatch only. Event-specific semantics, especially for tool and context hooks, stay in `core/chat/`.
- Hook-returned result dicts must still be valid stable result envelopes and JSON-serializable. Invalid override or patch dicts are ignored by chat integration rather than aborting the run.

## Constraints & Gotchas

- Extension modules run arbitrary in-process Python and therefore share the kernel's trust boundary.
- `context` hook safety is shallow-copy only at the chat integration point: top-level message dict mutations are isolated per request, but deep nested mutations can still affect shared objects.
- `tool_call` and `tool_result` semantics are defined by `core/chat/`, not by `ExtensionRegistry.fire()`: `tool_call` may short-circuit execution by returning a dict result envelope, and `tool_result` patches the live envelope with a shallow merge.