# Extensions

In-process Python extension hooks loaded by the runtime. Owns discovery, module loading, handler registration, and per-event hook dispatch; `core/chat/` decides only when each event fires and what its payload means.

## Overview

`core/extensions/` lets power users extend vBot without editing application source. Runtime loads Python modules from `<data_dir>/extensions/` plus optional extra scan roots from `settings.json` `extension_directories`, then passes a `HooksAPI` object into each extension's `register(api)` function. The module stores handlers, loads modules, **and dispatches every hook event** through typed per-event methods on `ExtensionRegistry`. `core/chat/` constructs the `HookContext`, chooses the fire-points, supplies each event's payload, and applies the returned results — it never iterates handlers itself. Extensions run in-process on the normal asyncio event loop when one exists.

## Data Model

- `HookContext` (frozen dataclass) — passed as the first positional argument to every handler. Constructed in `core/chat/` at each fire-point.
  - `session_id: str`, `agent_id: str`, `run_id: str`
  - `add_note: Callable[[str], None]` — appends a kernel-internal `role: "note"` entry to the active session. Chat wires it to `session.add_note`; extensions call `ctx.add_note("…")` to inject a `<system-reminder>` for the model (see the System-Reminder rules in PROJECT.md). Defaults to a no-op when built without a session.
- Decision types for `tool_call` (frozen dataclasses, importable from `core.extensions`): `Deny(reason: str)`, `Modify(input: dict)`, `Replace(result: dict)`. A handler returning `None` continues unchanged.
- `ToolCallDecision` — structured outcome `dispatch_tool_call` hands back to chat: `effective_input` (the input after any `Modify`), `deny_reason`/`deny_extension` (set when denied), `replacement` (a validated envelope when replaced). Exactly one disposition holds: proceed, denied, or replaced.
- `ExtensionRegistry`
  - `_handlers: dict[str, list[tuple[str, Callable]]]` — event name → ordered `(extension_name, handler)` pairs in load order. **Private to the registry**: only `HooksAPI.on` writes it and the registry's own `dispatch_*`/`_invoke` methods read it. No code outside `core/extensions/` touches it.
  - Event names are a fixed, closed set the registry dispatches and `core/chat/` fires: `run_start`, `before_agent_start`, `context`, `tool_call`, `tool_result`, `run_end`. Registering any other name is silently inert — no `dispatch_*` method reads it.

## Interfaces

- `HooksAPI(registry, extension_name)` — thin registration facade passed into an extension's `register(api)`.
- `HooksAPI.on(event, handler) -> None` — appends `(extension_name, handler)` to `registry._handlers[event]`. Every handler is called as `handler(ctx, **payload)`.
- `ExtensionRegistry.load(extensions_dir, extra_dirs=None) -> ExtensionRegistry` — scans immediate children of each root in order (`extensions_dir` first), imports them, and runs `register(api)` when present.
- `ToolResultValidator = Callable[[str, dict], dict | None]` — injected by chat into the tool hooks so tool-result-envelope schema knowledge stays in the chat domain. Given `(extension_name, candidate)` it returns the validated envelope or `None` to reject.
- **Per-event dispatch methods** (the event contract; one method per event). Each owns iteration in load order, sync/async handler support (awaits awaitables), per-handler exception isolation (log `warning`, continue via the `_HANDLER_FAILED` sentinel), and the event's composition rule:
  - `dispatch_run_start(ctx, *, session_id, agent_id) -> None` — observer; runs all, return values ignored.
  - `dispatch_run_end(ctx, *, session_id, agent_id, outcome) -> None` — observer; runs all, return values ignored.
  - `dispatch_before_agent_start(ctx, *, agent, session, messages, run) -> list[str]` — accumulator; returns every handler's `system_prompt_append` string in load order. Applying them to `messages[0]` stays in chat.
  - `dispatch_context(ctx, *, messages) -> list` — pipeline; threads the message list through every handler (a handler returning a list becomes the running list the next handler sees, any other return is inert), returns the final list. Chat passes a shallow per-message copy in and uses the result directly as the request messages.
  - `dispatch_tool_call(ctx, *, tool_name, tool_call_id, input, validator) -> ToolCallDecision` — decision pipeline; threads `input` through handlers applying `Modify`, short-circuits on the first `Deny`/valid `Replace`. `validator` validates a `Replace` envelope (invalid → log `warning`, treated as continue). Plain-dict returns are no longer honored (no legacy short-circuit branch).
  - `dispatch_tool_result(ctx, *, tool_name, tool_call_id, input, result, validator) -> dict` — replace pipeline; each handler returns a full replacement envelope (re-validated through `validator`, valid replaces the running envelope, invalid/`None`/non-dict are dropped) or `None`. No shallow-merge patching. Returns the final (possibly unchanged) envelope.
- Discovery accepts three entry-point shapes per immediate child:
  - single-file module: `<root>/<name>.py`
  - package module: `<root>/<name>/__init__.py`
  - directory fallback: `<root>/<name>/extension.py`
  - package entry points load under the synthetic `vbot_ext` namespace so relative imports inside extension packages work.

## Hook events (chat contract)

Each handler receives `ctx: HookContext` first, then the event kwargs below. Handlers may be sync or async; async results are awaited. The composition rule is owned by the matching `dispatch_*` method; the fire-point and payload meaning below are owned by `core/chat/`.

| Event | Composition | Payload (after `ctx`) | Allowed returns |
|---|---|---|---|
| `run_start` | observer | `session_id, agent_id` | ignored |
| `before_agent_start` | accumulator | `agent, session, messages, run` | `{"system_prompt_append": str}` |
| `context` | pipeline | `messages` | `list` (replaces running messages) or `None` |
| `tool_call` | decision pipeline | `tool_name, tool_call_id, input` | `None` / `Modify(input)` / `Deny(reason)` / `Replace(result)` |
| `tool_result` | replace pipeline | `tool_name, tool_call_id, input, result` | full replacement envelope `dict` or `None` |
| `run_end` | observer | `session_id, agent_id, outcome` | ignored |

- `run_start` — fired before the user turn is appended.
- `before_agent_start` — fired after request messages are built. The append string is added to the system message (`messages[0]`) only when its `content` is a string; all handlers run and appends accumulate in load order.
- `context` — fired before each provider request on a shallow per-message copy. Each handler in turn may return a list that becomes the running messages the next handler sees; the final running list is the request.
- `tool_call` — fired before a tool runs. `Modify` rewrites `input` and the pipeline continues (later handlers and the tool itself see the modified input). `Deny` stops the pipeline and the tool is not executed; chat builds a `tool_call_denied` failure envelope naming the denying extension. `Replace` stops the pipeline and skips execution; its envelope must pass the chat `validator` or it is logged and treated as continue. The modified arguments are what the tool executes with, what `tool_result` hooks receive, and what the `tool_call_started` timeline event shows; the persisted assistant `tool_calls` (the model's request) are untouched.
- `tool_result` — fired after the result is produced (real run, `Deny` envelope, or `Replace` envelope). Each handler returns a full replacement envelope, re-validated through the chat `validator`; valid replaces the running envelope (the next handler sees it), invalid or non-dict or `None` leaves it unchanged.
- `run_end` — fired in a `finally`, so it always runs. `outcome` is `"success"`, `"error"`, or `"cancelled"`.

## Conventions

- Extension discovery is shallow: only immediate children of each configured root are considered.
- Load order is deterministic by sorted extension name within each root; roots are processed in configured order.
- `register(api)` may be sync or async. Async registration is scheduled on the current running loop when startup already happens inside one; otherwise it is completed via `asyncio.run()`.
- Failures are fail-open. Load/`register()` failures log at `error`; per-event handler failures log at `warning` (`"Extension %r %s handler raised: %s"`) and never abort the run.
- Dispatch iteration, async/await handling, exception isolation, and all per-event composition semantics live in `core/extensions/`. `core/chat/` owns the fire-points, the `HookContext`, the payload meaning, and applying results (prompt appends, the `context` message copy, the tool-result-envelope `validator`).
- Hook-returned result envelopes (`Replace`, `tool_result` returns) must be valid stable result envelopes and JSON-serializable. Invalid envelopes are dropped by the `validator` rather than aborting the run.
- Decision objects (`Deny`/`Modify`/`Replace`) are exported from `core.extensions` because extensions run in-process and import them directly.

## Constraints & Gotchas

- Extension modules run arbitrary in-process Python and therefore share the kernel's trust boundary.
- `_handlers` is private to the registry. Cross-module code drives hooks through the `dispatch_*` methods; adding a new hook means adding a new event name plus a typed `dispatch_*` method here and a fire-point in `core/chat/`, not iterating `_handlers` elsewhere.
- `context` hook isolation is shallow-copy only: top-level message dict mutations are isolated per request, but deep nested mutations can still affect shared objects.
- `tool_call` is a decision pipeline (`Modify`/`Deny`/`Replace`); `tool_result` is a full-replace pipeline. Both validate envelopes through the chat-injected `validator` (tool-result envelope schema + JSON-serializability) and silently drop invalid ones. The `tool_call_started` event is emitted *after* the `tool_call` hook phase so it reflects modified arguments — this is later in the timeline than a pre-hook emission would be, by design.
- `settings.extension_directories` must be a list of non-empty strings; a non-list value is ignored with a warning, invalid entries are skipped, and paths are `expanduser()`-expanded.
