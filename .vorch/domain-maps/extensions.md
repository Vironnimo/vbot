# Extensions

In-process Python extension system loaded by the runtime. Owns discovery, two-phase registration (declare → apply), per-extension identity/config/lifecycle, and per-event hook dispatch; `core/chat/` decides only when each event fires and what its payload means.

## Overview

`core/extensions/` lets power users extend vBot without editing application source. An **extension** is the single unit of discovery, identity, config, and enable/disable; **hooks, tools, and recall backends** are the capability surfaces it uses (see Capability Surfaces). Runtime loads Python modules from `<data_dir>/extensions/` plus optional extra scan roots from `settings.json` `extension_directories`, passing in the disabled set and per-extension config read from the `settings.json` `extensions` section. `examples/extensions/` holds runnable, heavily-commented reference extensions (a `tool_call`-deny hook and a `register_tool` tool); `docs/extensions.md` is the user-facing author guide.

Loading is **two-phase**:

1. **Declare** — each extension's `register(api)` is called with an `ExtensionAPI`. Calls only *collect declarations* (hook handlers, startup/shutdown callbacks) into that extension's `ExtensionRecord`. Nothing goes live. Async `register()` coroutines are awaited to completion deterministically before phase 2.
2. **Apply** — after **all** extensions have registered, the registry installs every loaded extension's hook declarations into the dispatch table in load order.

The module then **dispatches every hook event** through typed per-event methods on `ExtensionRegistry`. `core/chat/` constructs the `HookContext`, chooses the fire-points, supplies each event's payload, and applies the returned results — it never iterates handlers itself. Extensions run in-process on the normal asyncio event loop when one exists. Trust boundary is the kernel's: extension modules run arbitrary in-process Python.

## Data Model

- `API_VERSION = 1` — exported from `core.extensions`. The extension contract version a manifest can pin against.
- `HookContext` (frozen dataclass) — passed as the first positional argument to every handler. Constructed in `core/chat/` at each fire-point.
  - `session_id: str`, `agent_id: str`, `run_id: str`
  - `add_note: Callable[[str], None]` — appends a kernel-internal `role: "note"` entry to the active session. Chat wires it to `session.add_note`; extensions call `ctx.add_note("…")` to inject a `<system-reminder>` for the model (see the System-Reminder rules in PROJECT.md). Defaults to a no-op when built without a session.
- Decision types for `tool_call` (frozen dataclasses, importable from `core.extensions`): `Deny(reason: str)`, `Modify(input: dict)`, `Replace(result: dict)`. A handler returning `None` continues unchanged.
- `ToolCallDecision` — structured outcome `dispatch_tool_call` hands back to chat: `effective_input` (the input after any `Modify`), `deny_reason`/`deny_extension` (set when denied), `replacement` (a validated envelope when replaced). Exactly one disposition holds: proceed, denied, or replaced.
- `ExtensionManifest` (frozen dataclass, optional) — parsed from `extension.json`: `version: str | None`, `description: str | None`, `api_version: int | None`, `display_name: str | None` (from the manifest `name` field, display-only). Identity stays the filesystem name.
- `ExtensionRecord` (one per discovered extension) — `name` (directory/file name, the identity), `root_path`, `entry_path`, `status` (`loaded` / `failed` / `disabled`), `error` (detail when `failed`, else `None`), `manifest` (or `None`), `declarations`, `capability_errors`. `declarations` are only meaningful for `loaded` records. `capability_errors` is a list of **non-fatal** per-capability diagnostics (e.g. one tool skipped on a name collision); the extension still `loaded` and is **not** in `diagnostics()` — only `status="failed"` records are. Plan 5 surfaces both.
- `ExtensionDeclarations` — what `ExtensionAPI` collects per extension: `hooks` (event → handler list), `startup`, `shutdown`, `tools` (`ToolDeclaration` list), `recall_backends` (`RecallBackendDeclaration` list). Internal; the registry applies/fires them. `ToolDeclaration` mirrors `ToolRegistry.register` args (`display` forwarded untyped so the module stays decoupled from `core/tools/`); `RecallBackendDeclaration` is `(name, factory)`.
- `ExtensionRegistry`
  - `_handlers: dict[str, list[tuple[str, Callable]]]` — event name → ordered `(extension_name, handler)` pairs in load order. **Private to the registry**: only `install_handler` (the apply primitive) writes it and the registry's own `dispatch_*`/`_invoke` methods read it. No code outside `core/extensions/` touches it.
  - `_records: list[ExtensionRecord]` — every discovered extension in load order.
  - Event names are a fixed, closed set the registry dispatches and `core/chat/` fires: `run_start`, `before_agent_start`, `context`, `tool_call`, `tool_result`, `run_end`. Registering any other name is silently inert — no `dispatch_*` method reads it.

## Interfaces

- `ExtensionAPI(extension_name, declarations, *, config, logger)` — registration facade passed into `register(api)`. Every call only collects a declaration; nothing is live until the apply phase.
  - `api.on(event, handler) -> None` — declare a hook handler. Called as `handler(ctx, **payload)`.
  - `api.register_tool(name, description, parameters, handler, *, internal=False, display=None) -> None` — declare an agent tool, mirroring `ToolRegistry.register`. Applied into the runtime `ToolRegistry` (see Capability Surfaces).
  - `api.register_recall_backend(name, factory) -> None` — declare a session-recall backend; `factory` is `RecallBackendContext -> RecallBackend`. Applied onto the recall registry.
  - `api.on_startup(handler) -> None` / `api.on_shutdown(handler) -> None` — declare a no-arg lifecycle handler (sync or async).
  - `api.config` — the extension's config object from `settings.extensions.config.<name>` (empty `dict` default).
  - `api.logger` — a `vbot.extensions.<name>` logger via the normal `LogManager` pipeline.
- `ExtensionRegistry.load(extensions_dir, extra_dirs=None, *, disabled=None, config=None) -> ExtensionRegistry` — scans immediate children of each root in order (`extensions_dir` first), runs the two-phase load, and returns the populated registry. Extensions named in `disabled` are recorded `disabled` and **never imported**. `config` maps extension name → its `api.config` object.
- `ExtensionRegistry.install_handler(extension_name, event, handler) -> None` — the apply phase's primitive: appends `(extension_name, handler)` to `_handlers[event]`. The single writer of `_handlers`; tests build a dispatch table through this seam without the filesystem load path.
- `ExtensionRegistry.apply_tools(tool_registry) -> None` / `apply_recall_backends(recall_registry) -> None` — the capability apply phases the runtime calls at the right bootstrap points (see Capability Surfaces). Both diagnose collisions/errors onto the record's `capability_errors` and fail open.
- `ExtensionRegistry.records() -> list[ExtensionRecord]` — every discovered record in load order.
- `ExtensionRegistry.diagnostics() -> list[ExtensionRecord]` — only the `failed` records (mirrors the skills `invalid_diagnostics()` idea). Runtime logs a startup warning when non-empty.
- `ExtensionRegistry.fire_startup() / fire_shutdown()` (async) — fire every loaded extension's startup/shutdown handlers in load order; fail-open per handler (log `error`, continue). `fire_shutdown_blocking()` runs `fire_shutdown()` to completion from synchronous shutdown paths.
- `ToolResultValidator = Callable[[str, dict], dict | None]` — injected by chat into the tool hooks so tool-result-envelope schema knowledge stays in the chat domain. Given `(extension_name, candidate)` it returns the validated envelope or `None` to reject.
- **Per-event dispatch methods** (the event contract; one method per event). Each owns iteration in load order, sync/async handler support (awaits awaitables), per-handler exception isolation (log `warning`, continue via the `_HANDLER_FAILED` sentinel), and the event's composition rule:
  - `dispatch_run_start(ctx, *, session_id, agent_id) -> None` — observer; runs all, return values ignored.
  - `dispatch_run_end(ctx, *, session_id, agent_id, outcome) -> None` — observer; runs all, return values ignored.
  - `dispatch_before_agent_start(ctx, *, agent, session, messages, run) -> list[str]` — accumulator; returns every handler's `system_prompt_append` string in load order. Applying them to `messages[0]` stays in chat.
  - `dispatch_context(ctx, *, messages) -> list` — pipeline; threads the message list through every handler (a handler returning a list becomes the running list the next handler sees, any other return is inert), returns the final list. Chat passes a shallow per-message copy in and uses the result directly as the request messages.
  - `dispatch_tool_call(ctx, *, tool_name, tool_call_id, input, validator) -> ToolCallDecision` — decision pipeline; threads `input` through handlers applying `Modify`, short-circuits on the first `Deny`/valid `Replace`. `validator` validates a `Replace` envelope (invalid → log `warning`, treated as continue). Plain-dict returns are no longer honored.
  - `dispatch_tool_result(ctx, *, tool_name, tool_call_id, input, result, validator) -> dict` — replace pipeline; each handler returns a full replacement envelope (re-validated through `validator`, valid replaces the running envelope, invalid/`None`/non-dict are dropped) or `None`. No shallow-merge patching. Returns the final (possibly unchanged) envelope.
- Discovery accepts three entry-point shapes per immediate child:
  - single-file module: `<root>/<name>.py` (`root_path == entry_path`; no manifest)
  - package module: `<root>/<name>/__init__.py`
  - directory fallback: `<root>/<name>/extension.py`
  - package entry points load under the synthetic `vbot_ext` namespace so relative imports inside extension packages work. The optional `extension.json` manifest lives in the directory for the package / directory-fallback shapes.

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

## Capability Surfaces (tools, recall backends)

Hooks are not the only thing an extension declares. `register(api)` also collects tool and recall-backend declarations; the runtime applies them into the **existing** domain registries (`ToolRegistry`, `RecallBackendRegistry`) — the extension API is a thin facade, not a new registry. Apply points are order-sensitive (tools late, recall early), so they live in `Runtime.start()`:

- **Tools** — `Runtime` calls `apply_tools(self._tools)` after the **last** built-in tool is registered (cron/bash/subagent/status), right before `SystemPromptManager` consumes the registry. Each declaration goes through the normal `ToolRegistry.register`, so an extension tool is a *normal* tool afterward: provider/prompt definitions, agent allowlists, and dispatch treat it like any other (no special-casing — see `.vorch/domain-maps/tools.md` and `.vorch/domain-maps/prompts.md`). Collision policy (load order must not silently decide behavior): a name already used by a **built-in** is skipped (built-in wins); between **two extensions** the first-loaded wins and **both** sides are diagnosed; a name already claimed by an earlier extension is skipped + diagnosed. Per-tool registration errors fail open. All skips/collisions land in the record's `capability_errors`.
- **Recall backends** — `Runtime._build_recall_backend_registry` applies `apply_recall_backends` onto a fresh `RecallBackendRegistry.with_builtins()` **before** `recall.backend` is resolved, and again on every `reload_recall_backend` (so extension backends survive a live switch). The registry's own rules hold: a duplicate name (built-ins register first) or a non lowercase-snake_case name raises `ValueError`, which is caught, diagnosed on the record, and the backend skipped. `Runtime.available_recall_backends()` returns the registry names (built-ins + extensions); the Settings Recall panel and `settings.update` validation read from it (see `.vorch/domain-maps/recall.md`).

Both apply phases only touch `loaded` records and never abort bootstrap.

## Visibility & Management

The extension records are surfaced through the normal accessor stack (CLI is an accessor — everything goes through server RPC):

- **`extensions.list` RPC** (`server/rpc/extensions_methods.py`) returns one entry per discovered record in load order: `name`, `status`, `disabled` (`status == "disabled"`), `root`/`entry` paths, `error`, `capability_errors`, manifest fields (`version`/`description`/`display_name`/`api_version`), the persisted `config` (merged from `settings.extensions.config`, read via `storage.load_extensions_settings()`), and a `capabilities` summary (`hooks` event→handler-count map, `tools`, `recall_backends`, `startup`/`shutdown` booleans). Empty list when no registry loaded.
- **CLI `vbot extensions list|enable|disable <name>`** (`cli/extensions_management.py`): `list` formats the RPC payload; `enable`/`disable` reconstruct the full `extensions` section from `extensions.list` and write it via `settings.update`, printing a restart hint when the response is `restart_required`. Unknown names get available-candidate + did-you-mean output; an already-enabled/disabled target is an idempotent no-op.
- **WebUI Settings → Extensions panel** (`SettingsExtensionsPanel.svelte`): renders the same data, a per-extension enable/disable toggle, a raw-JSON `config` editor, and a sticky restart-required notice after any change.
- **`settings.update({extensions})`** accepts `{disabled, config}` as a **full-replace** public section (see `.vorch/domain-maps/settings.md`); the response carries `"restart_required": true` because the change only takes effect at the next `Runtime.start()`.

## Lifecycle (startup/shutdown)

- **Startup** handlers fire once bootstrap is complete and an event loop is running. `Runtime.fire_extension_startup()` (async) delegates to `registry.fire_startup()`; the server calls it from inside its async lifespan after `start()`, so handlers run on the live serving loop and may schedule background tasks there. Accessors that never serve (CLI local commands) do not fire startup.
- **Shutdown** handlers fire during runtime shutdown **before** `Runtime._clear_service_references`: `Runtime.aclose()` awaits `registry.fire_shutdown()`; the synchronous `Runtime.stop()` uses `registry.fire_shutdown_blocking()`.
- Both phases fail-open per handler (log `error`, continue). Only `loaded` records' handlers fire.

## Conventions

- Extension discovery is shallow: only immediate children of each configured root are considered.
- Load order is deterministic by sorted extension name within each root; roots are processed in configured order. The apply phase installs hook declarations in that same order, so `_handlers` ordering matches discovery order.
- Identity is the filesystem name. `extension.json` is optional and never required; single-file extensions are first-class and cannot carry one. A manifest `name` is display-only.
- `register(api)` may be sync or async. Async `register()` coroutines are gathered and awaited to completion **before** the apply phase — no fire-and-forget. `_run_coroutine_to_completion` drives them directly when no loop runs, and on a private loop in a worker thread when a loop is already running (e.g. `Runtime.start()` inside the server lifespan), keeping both situations deterministic.
- Failures are fail-open and recorded on the `ExtensionRecord`: disabled extensions are never imported (`status="disabled"`); import / `register()` / manifest errors produce `status="failed"` with an `error` detail (logged at `error`) and never abort the other extensions. Per-event handler failures log at `warning` (`"Extension %r %s handler raised: %s"`); lifecycle handler failures log at `error`. None abort the run.
- A manifest `api_version` greater than `API_VERSION` fails the extension before import with a clear message.
- Dispatch iteration, async/await handling, exception isolation, and all per-event composition semantics live in `core/extensions/`. `core/chat/` owns the fire-points, the `HookContext`, the payload meaning, and applying results (prompt appends, the `context` message copy, the tool-result-envelope `validator`).
- Hook-returned result envelopes (`Replace`, `tool_result` returns) must be valid stable result envelopes and JSON-serializable. Invalid envelopes are dropped by the `validator` rather than aborting the run.
- Decision objects (`Deny`/`Modify`/`Replace`) and `API_VERSION` are exported from `core.extensions` because extensions run in-process and import them directly.

## Constraints & Gotchas

- Extension modules run arbitrary in-process Python and therefore share the kernel's trust boundary.
- `_handlers` is private to the registry; only `install_handler` (the apply phase / test seam) writes it. Cross-module code drives hooks through the `dispatch_*` methods; adding a new hook means adding a new event name plus a typed `dispatch_*` method here and a fire-point in `core/chat/`, not iterating `_handlers` elsewhere.
- Enable/disable and per-extension config are **restart-applied**: the `settings.extensions` section is read only at `Runtime.start()`. The public `settings.update({extensions})` section persists changes but does **not** hot-reload (handlers may be mid-flight) — it returns `restart_required` so an accessor can prompt for `vbot server restart`.
- `context` hook isolation is shallow-copy only: top-level message dict mutations are isolated per request, but deep nested mutations can still affect shared objects.
- `tool_call` is a decision pipeline (`Modify`/`Deny`/`Replace`); `tool_result` is a full-replace pipeline. Both validate envelopes through the chat-injected `validator` (tool-result envelope schema + JSON-serializability) and silently drop invalid ones. The `tool_call_started` event is emitted *after* the `tool_call` hook phase so it reflects modified arguments — this is later in the timeline than a pre-hook emission would be, by design.
- `settings.extension_directories` must be a list of non-empty strings; a non-list value is ignored with a warning, invalid entries are skipped, and paths are `expanduser()`-expanded. The `settings.extensions` section (`disabled` list + `config` map) is validated centrally in `core/settings/validation.py`; the runtime re-parses it defensively and ignores malformed pieces with a warning.
