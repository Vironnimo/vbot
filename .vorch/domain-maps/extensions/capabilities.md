# Extension Capabilities

Read this reference only when adding or changing an Extension capability or its runtime dispatch. The always-read identity, trust, loading, and lifecycle invariants live in `extensions.md`.

## Declaration model

Every `ExtensionAPI` registration method only appends to that Extension's `ExtensionDeclarations`. Hooks and channel interaction handlers are applied by `ExtensionRegistry.load()` after every Extension has registered; Commands, Tools, Recall backends, and Prompt blocks are applied later by Runtime at the owning registry's bootstrap point.

`HookContext` is the common frozen context for Chat hooks: `session_id`, `agent_id`, `run_id`, and `add_note(text)`. Chat constructs it and wires `add_note` to the active Session's kernel-note path; a context constructed without a Session uses a no-op sink.

## Chat hooks

The hook set is closed: `run_start`, `context`, `tool_call`, `tool_result`, and `run_end`. Adding another name to `api.on()` is inert unless `ExtensionRegistry` exposes a dispatch path and Chat fires it at a defined boundary.

- `run_start(ctx, *, session_id, agent_id)` fires once after the Run is admitted and before context assembly.
- `context(ctx, *, messages)` is an ordered transform pipeline. A handler returning a list replaces the effective messages for subsequent handlers; `None`, an invalid result, or a raised exception leaves the current messages unchanged.
- `tool_call(ctx, *, tool_name, tool_call_id, input)` is an ordered decision pipeline. `Modify(input)` changes the effective input for later handlers and execution; `Deny(reason)` stops execution with the denying Extension recorded; `Replace(result)` stops execution only after Chat validates the candidate Tool result envelope. `None`, invalid returns, and isolated handler failures continue.
- `tool_result(ctx, *, tool_name, tool_call_id, input, result)` is an ordered replacement pipeline over the normalized Tool result. A valid full result-envelope dict replaces the running result for later handlers; `None`, invalid envelopes, and isolated failures leave it unchanged. There is no shallow-merge patching.
- `run_end(ctx, *, session_id, agent_id, outcome)` fires once on Run completion with Chat's terminal outcome.

Chat owns exact fire-points, `Replace` validation, Tool lifecycle events, and how decisions become model-visible results. Changes therefore require reading `chat/run-execution.md` and the relevant tests as well as this reference.

Chat passes `context` a shallow copy of each top-level message dict. Replacing or mutating those dicts is request-local, but mutating nested objects can still affect shared values. The public `tool_call_started` lifecycle event is emitted after the `tool_call` decision pipeline so its arguments reflect any accepted `Modify`.

## Slash Commands

`api.register_command(name, description, handler, *, argument="optional", catalog_result="notice", execution_mode="serialized", argument_execution_mode=None, unavailable_surfaces=())` declares a normal Slash Command. `ExtensionRegistry.apply_commands()` applies loaded declarations in deterministic load order to Runtime's stable `CommandDispatcher`; Built-ins always win, the first Extension wins cross-Extension collisions, and duplicates or malformed declarations are skipped with non-fatal diagnostics.

The sync or async handler receives `(ExtensionCommandContext, raw_argument)` and must return a matching `CommandOutcome`. The context carries current Agent/Session/Project addressing, `ReplySurface`, generic change reporting, and a narrow `start_run()` helper bound through `TriggerService`; it never exposes Runtime. Handler failures and invalid outcomes are logged with ownership and become neutral failure feedback. Each applied registration has an opaque identity copied into `PreparedCommand`, so work queued before removal or replacement returns neutral stale feedback instead of calling dormant or newly shadowing code. Full recognition, scheduling, outcome, RPC, and Channel contracts live in `chat/commands.md`.

## Tools

`api.register_tool(name, description, parameters, handler, *, internal=False, display=None, ready=None, readiness_hint=None, result_schema=None, parallel_safe=True)` declares a normal Tool. Runtime applies Extension Tools after built-ins into the same `ToolRegistry`; they then use the same compiled canonical input contract, optional success-data contract, fingerprint, allowlist, Prompt, Provider definition, dispatch, and result-envelope contracts as built-ins. Invalid Tool registration is diagnosed and skipped without preventing that Extension's other capabilities from applying.

An existing built-in or earlier Extension Tool name wins. The losing declaration is skipped and diagnosed without failing the Extension. `ready` must be cheap and I/O-free; a false result leaves the Tool registered but hides it from model-facing surfaces. `readiness_hint` is optional English guidance exposed by `tool.list`. Extension Tool siblings run concurrently by default within shared limits; `parallel_safe=False` declares a whole-Tool ordering barrier. Any active `tool_call` or `tool_result` hook forces all sibling Tool Calls to serialize because hook transformations and replacements are not assumed commutative.

## Recall backends

`api.register_recall_backend(name, factory)` declares a `RecallBackendContext -> RecallBackend` factory. Runtime applies it to a registry with built-ins already present before resolving the persisted backend. Invalid or duplicate names are diagnosed and skipped; the Recall domain owns result-unit, control, ranking, pagination, and snapshot semantics.

If a live-disabled Extension supplied the active backend, Runtime rebuilds Recall and falls back through the normal unknown-backend path while leaving the persisted selection intact, so re-enabling can restore it.

## System Prompt blocks

`api.register_prompt_block(slug, *, default_text=None, render=None)` requires exactly one content mode: static editable default text or dynamic build-time rendering. Runtime converts declarations to `BlockDefinition` values with id `extension:<slug>` and owner `extension:<extension-name>` and hands them to the Prompts domain.

Only loaded owners contribute. Slug/id collisions are first-wins and diagnosed; a bad declaration or raising dynamic renderer drops the affected block rather than the Extension layer. Prompt ordering, overrides, render gates, and composition remain owned by `prompts.md`.

## Channel interaction handlers

`api.register_interaction_handler(prefix, handler)` declares deterministic in-process handling for callback data beginning `"<prefix>:"`. The neutral contract in `core/extensions/interactions.py` keeps the dependency channels → extensions:

- `InteractionButton(label, data)` is one keyboard button.
- `InteractionEvent` carries platform/channel/chat/user/message identity, callback `data`, the current keyboard snapshot, and optional message/user/thread data.
- `InteractionResponder.answer()` acknowledges the tap; `edit()` changes the tapped message text and/or keyboard.

`dispatch_channel_interaction(event, responder)` routes by the prefix before the first colon. A matched handler returns handled even if it raises; dispatch logs and swallows the failure. Prefix collisions are first-wins and diagnosed. The runtime-reserved `run` prefix is rejected before collision handling because Channels routes it to an Agent Run instead.

Every tap must be acknowledged exactly once from the user's perspective. The channel adapter guarantees a fallback acknowledgement if an Extension handler does not answer.

The bundled Checklist Extension is the small reference implementation: it owns prefix `chk`, toggles the tapped button's leading ⬜/✅ label, edits the whole keyboard from the event snapshot, and silently acknowledges. It persists no state; concurrent taps can temporarily overwrite from stale platform snapshots and self-heal on a later tap.

## Capability teardown

Live disable removes the Extension's hook and interaction entries, unregisters its applied Commands and only Tools whose live handler identity matches the declaration, fires shutdown, and clears declarations. Full reload removes old Command registrations and uses identity-safe Tool removal before discarding the entire old registry. Collision-skipped capabilities must never be removed from their real owner.

## Source and tests

- Contracts and application: `core/extensions/extensions.py`, `core/extensions/interactions.py`
- Chat integration: `core/chat/chat.py`, `core/chat/commands.py`, `core/chat/tool_dispatch.py`
- Runtime application/teardown: `core/runtime/runtime.py`
- Focused coverage: `tests/core/extensions/test_capabilities.py`, `test_dispatch.py`, `test_interactions.py`, `test_deactivate.py`, and relevant Chat/Runtime tests
