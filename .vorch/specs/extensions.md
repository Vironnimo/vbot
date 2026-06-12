# Extensions

In-process Python extension hooks loaded by the runtime. Owns discovery, module loading, handler registration, and per-event hook dispatch; `core/chat/` decides only when each event fires and what its payload means.

## Overview

`core/extensions/` lets power users extend vBot without editing application source. Runtime loads Python modules from `<data_dir>/extensions/` plus optional extra scan roots from `settings.json` `extension_directories`, then passes a `HooksAPI` object into each extension's `register(api)` function. The module stores handlers, loads modules, **and dispatches every hook event** through typed per-event methods on `ExtensionRegistry`. `core/chat/` constructs the `HookContext`, chooses the fire-points, supplies each event's payload, and applies the returned results — it never iterates handlers itself. Extensions run in-process on the normal asyncio event loop when one exists.

## Data Model

- `HookContext` (frozen dataclass) — passed as the first positional argument to every handler. Constructed in `core/chat/`.
  - `session_id: str`
  - `agent_id: str`
- `ExtensionRegistry`
  - `_handlers: dict[str, list[tuple[str, Callable]]]` — event name → ordered `(extension_name, handler)` pairs in load order. **Private to the registry**: only `HooksAPI.on` writes it and the registry's own `dispatch_*`/`_invoke` methods read it. No code outside `core/extensions/` touches it.
  - Event names are a fixed, closed set the registry dispatches and `core/chat/` fires: `run_start`, `before_agent_start`, `context`, `tool_call`, `tool_result`, `run_end`. Registering any other name is silently inert — no `dispatch_*` method reads it.

## Interfaces

- `HooksAPI(registry, extension_name)` — thin registration facade passed into an extension's `register(api)`.
- `HooksAPI.on(event, handler) -> None` — appends `(extension_name, handler)` to `registry._handlers[event]`. Every handler is called as `handler(ctx, **payload)`.
- `ExtensionRegistry.load(extensions_dir, extra_dirs=None) -> ExtensionRegistry` — scans immediate children of each root in order (`extensions_dir` first), imports them, and runs `register(api)` when present.
- `ToolResultValidator = Callable[[str, dict], dict | None]` — injected by chat into the tool hooks so tool-result-envelope schema knowledge stays in the chat domain. Given `(extension_name, candidate)` it returns the validated envelope or `None` to reject.
- **Per-event dispatch methods** (the event contract; one method per event, signatures mirror today's payloads). Each owns iteration in load order, sync/async handler support (awaits awaitables), per-handler exception isolation (log `warning`, continue via the `_HANDLER_FAILED` sentinel), and the event's composition rule:
  - `dispatch_run_start(ctx, *, session_id, agent_id) -> None` — observer; runs all, return values ignored.
  - `dispatch_run_end(ctx, *, session_id, agent_id, outcome) -> None` — observer; runs all, return values ignored.
  - `dispatch_before_agent_start(ctx, *, agent, session, messages, run) -> list[str]` — accumulator; returns every handler's `system_prompt_append` string in load order. Applying them to `messages[0]` stays in chat.
  - `dispatch_context(ctx, *, messages) -> list | None` — first-wins pipeline; returns the first handler's replacement list, or `None` when none replaced (chat then keeps its own copy).
  - `dispatch_tool_call(ctx, *, tool_name, tool_call_id, input, validator) -> dict | None` — decision pipeline; first handler dict that passes `validator` short-circuits and is returned, else `None`.
  - `dispatch_tool_result(ctx, *, tool_name, tool_call_id, input, result, validator) -> dict` — merge pipeline; each handler dict is shallow-merged onto the running envelope and re-validated, valid patches replace it, invalid ones are dropped; returns the final (possibly unchanged) envelope.
- Discovery accepts three entry-point shapes per immediate child:
  - single-file module: `<root>/<name>.py`
  - package module: `<root>/<name>/__init__.py`
  - directory fallback: `<root>/<name>/extension.py`
  - package entry points load under the synthetic `vbot_ext` namespace so relative imports inside extension packages work.

## Hook events (chat contract)

Each handler receives `ctx: HookContext` first, then the event kwargs below. Handlers may be sync or async; async results are awaited. The composition rule is owned by the matching `dispatch_*` method; the fire-point and payload meaning below are owned by `core/chat/`.

- `run_start(ctx, session_id, agent_id)` — fired before the user turn is appended. Return value ignored.
- `before_agent_start(ctx, agent, session, messages, run)` — fired after request messages are built. May return `{"system_prompt_append": str}`; the string is appended to the system message (`messages[0]`), but only when that message's `content` is a string. All handlers run and appends accumulate.
- `context(ctx, messages)` — fired before each provider request, on a shallow per-message copy. May return a `list` to fully replace the request messages; the first handler that returns a list wins and the rest are skipped.
- `tool_call(ctx, tool_name, tool_call_id, input)` — fired before a tool runs. May return a dict result envelope to short-circuit execution; the first valid envelope wins and the tool is not dispatched. The dict must be a valid, JSON-serializable tool-result envelope or it is ignored.
- `tool_result(ctx, tool_name, tool_call_id, input, result)` — fired after a tool runs. A returned dict is shallow-merged onto the live envelope and re-validated; all handlers run in turn so each can patch, and each handler sees the running (already-patched) envelope. Invalid patches are ignored and the prior result is kept.
- `run_end(ctx, session_id, agent_id, outcome)` — fired in a `finally`, so it always runs. `outcome` is `"success"`, `"error"`, or `"cancelled"`. Return value ignored.

## Conventions

- Extension discovery is shallow: only immediate children of each configured root are considered.
- Load order is deterministic by sorted extension name within each root; roots are processed in configured order.
- `register(api)` may be sync or async. Async registration is scheduled on the current running loop when startup already happens inside one; otherwise it is completed via `asyncio.run()`.
- Failures are fail-open. Load/`register()` failures log at `error`; per-event handler failures log at `warning` (`"Extension %r %s handler raised: %s"`) and never abort the run.
- Dispatch iteration, async/await handling, exception isolation, and all per-event composition semantics live in `core/extensions/`. `core/chat/` owns the fire-points, the `HookContext`, the payload meaning, and applying results (prompt appends, the `context` message copy, the tool-result-envelope `validator`).
- Hook-returned result dicts must be valid stable result envelopes and JSON-serializable. Invalid override or patch dicts are dropped by the `validator` rather than aborting the run.

## Constraints & Gotchas

- Extension modules run arbitrary in-process Python and therefore share the kernel's trust boundary.
- `_handlers` is private to the registry. Cross-module code drives hooks through the `dispatch_*` methods; adding a new hook means adding a new event name plus a typed `dispatch_*` method here and a fire-point in `core/chat/`, not iterating `_handlers` elsewhere.
- `context` hook isolation is shallow-copy only: top-level message dict mutations are isolated per request, but deep nested mutations can still affect shared objects.
- `tool_call` short-circuits with the first valid envelope; `tool_result` lets every handler shallow-merge-patch the envelope in turn. Both validate through the chat-injected `validator` (tool-result envelope schema + JSON-serializability) and silently drop invalid dicts.
- `settings.extension_directories` must be a list of non-empty strings; a non-list value is ignored with a warning, invalid entries are skipped, and paths are `expanduser()`-expanded.
