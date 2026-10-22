# Extension Management

Read this reference only for Extension discovery, manifests (including `config_documents` validators), records, settings schemas, secrets, visibility, lifecycle, enable/disable, or full reload work, or for what each `API_VERSION` added. Capability semantics live in `extensions/capabilities.md`.

## Discoverable shapes and manifests

Each scan root accepts immediate `.py` files and directories with an Extension entry point. Import names live under the synthetic `vbot_ext` package; both directory entry points (`__init__.py` and `extension.py`) support relative sibling imports. Identity is the file stem or directory name; the optional directory manifest enriches display metadata but never replaces filesystem identity.

`ExtensionRegistry.aload`, used by live full reload, runs discovery, manifest reads, imports and synchronous `register(api)` on the dedicated single-worker `extension-load` pool. These operations collect an unpublished registry; synchronous registration has no running Event Loop. Async `register(api)` bodies, including coroutines returned by synchronous registration, run one at a time on the serving loop with a hard 10-second deadline. A timeout marks only that Extension as failed and detaches the cancellation request, so cancellation-resistant registration no longer gates the load. After registration, page paths resolve on the loader worker; declarations and page routing publish together on the serving loop without another await. The synchronous `ExtensionRegistry.load` (initial Runtime bootstrap, tests and tooling) keeps the private-event-loop daemon-worker join path for async registration.

A registration that cancels itself fails only its Extension. Cancelling a loader waiting for worker admission starts no scan. Cancelling an admitted scan waits for the worker to settle, then closes its unstarted registration coroutines without publishing the registry. During async registration, loader cancellation requests cancellation of its active registration and closes registrations that have not started; later failures from detached registration work remain observable in the Extension log. Runtime's mutation boundary still finishes an admitted reload before propagating its caller's cancellation.

`settings.extension_directories` is the only configurable extra-root list; the bundled root is fixed separately and cannot be removed through that setting. Runtime ignores a non-list value with a warning, skips non-string or empty entries, expands `~`, and preserves configured order before the final bundled root.

`ExtensionRecord` retains every discovered result in load order: identity and paths, `status`, load `error`, optional manifest, collected declarations, non-fatal `capability_errors`, and `overridden_by` for shadowed copies. `records()` includes all statuses; `diagnostics()` includes only failed records.

The load path must preserve first-wins root shadowing before import. Overridden copies are not imported and their manifests are not read. Disabled winners are recorded without import but still shadow later roots.

A missing `extension.json` is optional; an unavailable or unreadable manifest fails its Extension before import, so its API-version requirement cannot disappear on an I/O failure.

## API versions

What each `API_VERSION` (`extensions.md` -> Loading and identity invariants) added:

- v2: Slash Command declarations. v3: Extension-owned Tool Family declarations. v4: management operations, host capabilities, and live Tool catalogs. v5: explicit opt-in Tools. v6: page declarations and explicit Tool catalog visibility.
- v7: owner-bound Extension databases (`host.open_database`, `extensions/databases.md`). v8: Tool result payloads (`ToolContext.attach_result_payload`, `host.load_result_payload`, `extensions/host.md`). v9: Tool-gated prompt blocks (`register_prompt_block(..., requires_tool=...)`, `extensions/capabilities.md`). v10: owner group listing and archiving (`temporary_agents.groups()`, `archive_group()`). v11: OAuth browser redirects (`host.oauth_redirects`, `extensions/host.md`).
- v12: server folder listing for pages (`extensionPageClient.listDirectory`, `webui/extension-pages.md` -> Capabilities). v13: Tool summaries (`api.register_tool(..., summary=...)`, `tools/load_tools.md` -> Tool summaries). v14: replaces `definition_change_note` with declared Tool-change announcements (`announce_changes`, `tools.md` -> Tool changes in a Session): a changed Tool definition reaches a Session mid-epoch only for the parts its Tool names. v15: asynchronous Tool-Agent resolution, canonical deferred-call receipts (`extensions/host.md` -> Deferred Tool outcomes) and manifest-declared offline document validators (below).
- v16: windowed Run steps for pages (`history.read` `query.child_limit`, `run.subscribe` `child_limit`, `extensionPageClient.readRunChildren`; `webui/extension-pages.md` -> Run streams and history).
- v17: Secret Sources for Agent Secrets (`api.register_secret_source`, a `core.secrets.sources.SecretSource`; `secrets/agent-secrets.md` -> Secret Sources).
- v18: `host.resolve_permissions(project_id, agent_id)`, the Permission Boundary of a new Session of an Agent, for Tool calls an Extension makes as that Agent outside any Session (`extensions/host.md`).
- v19: Tool rows of page History and Run streams carry bounded previews of their call (`rawTruncated`); `history.tool_call` (`extensionPageClient.readToolCall`) reads the whole call (`webui/extension-pages.md` -> Run streams and history).
- v20: argument hints on slash Commands (`api.register_command(..., argument_hint=...)`, `chat/commands.md`).

## Offline document validation

API 15 directory Extensions declare `config_documents` in `extension.json`: a mapping from owner-state-relative glob patterns to `module:function` file validators. Core `doctor config` discovers these in normal first-wins root order, even when disabled, then loads only the named module under an isolated package path; it never runs the Extension's `__init__`, `register` or startup. The callable receives the absolute Path and returns `JsonValidationReport`. Patterns cannot escape the owner's state directory; validator module paths cannot start with `__init__`. Declaration/import/validation failures become diagnostics; an in-scope known document without its owner's validator also reports an error. Implementation: `core/extensions/config_validation.py`; evidence: `tests/core/settings/test_validation.py`.

## Settings schemas and live values

`api.register_settings(fields)` declares at most one schema. `settings_schema.py` parses field keys, labels/descriptions, required/default metadata, and the supported `text`, `number`, `toggle`, and `secret` types. Invalid declarations fail registration with a field-specific error.

Non-secret values live in `settings.extensions.config.<extension>` and are read live through `api.get_config()`. The Settings RPC layer validates config for loaded schema-owning Extensions before persistence because that layer has both the registry and the update; core Settings storage continues to own only the generic section shape.

A secret declaration supplies an explicit `env_key`, cannot carry a normal default, and never enters the JSON config. `extensions.set_secret` accepts Extension name, schema field key, and a string value; it resolves the declared field server-side, writes or clears the data-dir credential, reloads Provider credentials, and returns only `{name, key, set}`. Process-environment precedence still applies to live resolution.

## Visibility contract

`extensions.list` and `extensions.reload` return the same `{extensions: [...], settings: {...}}` catalog shape. `settings` is the persisted `extensions` section as `settings.update` writes it (`disabled` in saved order, `config`), including entries of Extensions without a record, so a writer can change one entry and send the section it read as `base` (`webui/autosave.md`). Each record projection includes status/path/error/shadowing/manifest metadata, persisted non-secret config, loaded schema metadata, capability summary, and derived readiness; secret fields expose only their `env_key` and live `set` boolean. Declared Commands are projected with their name and whether the stable live `CommandDispatcher` currently registers that name to this Extension, so collision-skipped declarations remain visible without being reported as active.

Catalog projection reads persisted Settings on its bounded worker, then captures
the current registry and evaluates live readiness on the Event Loop. Catalog reads,
explicit reload and secret edits share the Settings mutation lock with enable/disable.

Readiness is a display projection, not a stored Extension status. A loaded Extension with declared Tools is `waiting` when at least one currently applied Tool is not ready; otherwise it is `ready`. A collision-skipped Tool is not applied and therefore not ready.

## Enable, disable, and configuration decisions

The disabled set and config are persisted together under Settings, but their runtime consequences differ:

- Config-only change: persist and return; Extension handlers read new values through `get_config()` with no rebuild.
- Disable-only change: under the ExtensionRuntime mutation lock, deactivate newly disabled loaded records, remove their applied Commands, refresh Prompt blocks and Extension-bundled Skills, and recover the active Recall backend if necessary.
- Any newly enabled name: perform one full `reload_extensions()` after persistence. A mixed enable/disable save needs no second disable pass because the rebuild reads the final persisted set.
- Explicit reload: rebuild the whole layer even when the disabled set did not change, so disk edits/additions/deletions and repaired failures become visible.

Successful explicit reload and Settings mutations that reload/enable/disable Extensions publish `resource_changed(kind="commands")` and then an unscoped `resource_changed(kind="extensions")` (page descriptors) after the structural work. Config-only saves publish neither because per-call values change without altering the Command catalog or page registrations.

## Full reload sequence

`Runtime.reload_extensions()` runs on the serving loop under the ExtensionRuntime mutation lock: read fresh roots/settings; quiesce the old registry and retire it (Hooks, interaction handlers, owner hosts, page/management operations and Session capabilities stop reaching old Extensions while the replacement loads; page RPCs fail with a refresh-the-page error, Hooks simply do not fire); detach old Extension Tools and Commands; await old shutdown; purge all `vbot_ext` modules; load a fresh registry; swap it in; reapply Tools and Commands to their stable owners; rebuild Recall, Prompt blocks, and Skills against the new loaded set; archive the owner-managed Sessions of Extensions no longer installed (`extensions/host.md` -> Owner-managed Session lifetime); then await new startup.

While the runtime is closing, reload and enable/disable changes, like a lookup of an unbound owner host, raise `ExtensionUnavailableError`, an expected `VBotError`: RPC reports it as `domain_error`, and `GET /api/extension-runs/{token}/events` answers 404 for it like an unknown token, instead of an internal error.

Once admitted, reload, live disable, and startup finish their structural work before propagating caller cancellation; cancellation while waiting for admission makes no changes. A later mutation failure is logged with its operation and traceback before caller cancellation propagates. Async Runtime shutdown closes Extension mutation admission and drains the admitted operation under the same lock before shutting down the final registry and clearing service references.

Reload and live disable read their persisted Settings on a bounded worker and pass
that snapshot into Recall recovery/rebuild; live backend and registry changes stay
on the Event Loop. They await the bounded asynchronous Skill scan before completing structural work. Registry installation remains on the Event Loop, and cancellation/shutdown wait for that publication.

The rebuild is restart-equivalent for the Extension layer, not atomic Run draining. A concurrently executing handler may finish against old code; normal per-handler fail-open isolation is the accepted boundary. Prepared Extension Commands carry a registration identity, so deferred Channel work that has not started execution cannot invoke removed code or a replacement owner.

## Startup and shutdown

For packaged installs, the application owner resolves one explicit local requirements recipe into a data-dir managed site. It constrains every distribution to the immutable runtime inventory, removes same-version runtime duplicates from the target, and records the complete recipe with its compatibility-scoped site. Runtime appends that site only during normal startup, never through `.pth` execution; its full inventory plus Python ABI fingerprint must match. A changed recipe is effective after restart, not an Extension Reload. Invalid optional dependencies leave unrelated Extensions loadable. A development checkout has neither the release manifest nor the runtime inventory and loads Extensions without that site (`core/extensions/dependencies.py::activate`).

Authoring convention: keep imports and `register(api)` free of resource acquisition; acquire connections, tasks and file handles in startup, release them in shutdown, and make both handlers idempotent because reload cycles every loaded owner. This guidance is also part of the bundled `vbot-docs` authoring reference.

Loaded Extensions fire startup in load order after runtime capability application; initial startup, like reload, first archives the owner-managed Sessions of Extensions no longer installed. Runtime stop fires shutdown for loaded records; live reload awaits old shutdown and new startup on the serving loop; live disable fires only that record's shutdown. After each owner's shutdown handlers, the registry releases that registration through the root host's `release_owner`, closing the databases it opened with `host.open_database` (`extensions/databases.md`). Synchronous and asynchronous lifecycle handlers share fail-open logging and do not prevent remaining handlers from running; a failed release is logged the same way. A callback that cancels itself is a callback failure, logged without skipping the remaining handlers; cancellation of the lifecycle caller still cancels its active callback and propagates.

Lifecycle waits are bounded like registration, because reload, disable and shutdown hold the ExtensionRuntime mutation lock (and a Settings save that triggered them keeps the Settings mutation lock for them, `settings.md` -> Constraints & Gotchas). Each shutdown path drains an owner (`quiesce`) before its shutdown handlers run, so shutdown handlers need not drain again; Swarm's `close()` relies on it. `invoke_lifecycle_handler` in `_callbacks.py` stops waiting after `_LIFECYCLE_HANDLER_TIMEOUT_SECONDS` (30 s) and logs an ERROR `... timed out after 30 seconds`:

- A startup or shutdown handler past the deadline is asked to cancel and detached; the next handler runs. Owner release still follows shutdown.
- A Session owner's `quiesce` past the deadline is detached without cancellation and the owner counts as quiesced, so removal proceeds. Cancelling could stop the drain before it cancels the owner's Runs. The drain keeps retiring admissions and cancelling owned Runs in the background; the retired registration refuses their late Session work.
- Detached callbacks stay referenced, and a later failure is still logged (`Extension ... detached ... raised`); async registration uses the same detachment. Cancelling the caller cancels the callback and propagates. A synchronous handler blocked in its worker thread cannot be stopped: it keeps that worker until it returns. `fire_shutdown_blocking` runs on its own event loop, whose close still waits for detached tasks that ignore cancellation.

Coverage: `test_lifecycle_handlers_fire_in_load_order_and_fail_open` (`test_registration.py`) and `test_deactivate_quiesces_before_removing_capabilities_and_retires_host` (`test_deactivate.py`).

## Source and tests

- Discovery/import/registration deadlines: `core/extensions/_loading.py`; records: `_declarations.py`; schema declarations: `_api.py` and `settings_schema.py`; lifecycle and public API: `extensions.py` (internal files under `core/extensions/`).
- Serialized lifecycle mutation: `core/extensions/runtime.py`; Runtime readiness and service teardown: `core/runtime/runtime.py`
- RPC catalog/secrets and Settings persistence: `server/rpc/extensions_methods.py`, `server/rpc/settings_methods.py`; post-write live Settings coordination: `core/runtime/_settings.py`
- Focused coverage: `tests/core/extensions/test_loader.py`, `test_registration.py`, `test_settings_schema.py`, `test_runtime.py`, `test_deactivate.py`, and Extension RPC/Runtime tests


## Managed operations and live catalogs

API v4 adds `api.operations` (`core/extensions/operations.py`). Operations declare argument schemas, async handlers, and a secret-input marker; `extensions.operation` validates before invocation and exposes schema paths rather than rejected credential values. CLI discovers this catalog instead of adding Extension-specific parser branches. Operation and page RPCs map typed domain failures through the common server mapper; expired Session capabilities use `session_capability_expired` with a page-reload recovery message. Unexpected exceptions propagate to the HTTP internal-error envelope, and cancellation remains cancellation (`tests/server/rpc/test_extensions_methods.py`, `tests/server/test_app_http.py`). Optional pending-input callbacks feed `extensions.requests`; responses use the declared operation through the same RPC boundary. An Extension that declares `pending_inputs` publishes a change with resource `PENDING_INPUTS_RESOURCE` (`"pending_inputs"`, defined next to `ExtensionOperations`) through its owner-bound `host.publish_change` whenever that list gains or loses an entry, naming the added or removed request ids; accessors read `extensions.requests` again on exactly these changes and on owner-less `extensions` invalidations instead of polling it (MCP: `test_input_response_is_validated_and_not_retained`). `ExtensionRequests.svelte` is mounted globally because input can arrive while Chat is active.

Managed startup receives an injected `ExtensionHost` with data-directory, credential, Agent/cwd resolution, attachment storage, and Model sampling capabilities. Runtime binds this host before startup and on reload. `replace_tools` stages a complete catalog and checks exact prior ownership before publication; followers require source Tools from the same Extension in the resulting registry, including sources in another owned catalog, so a replacement cannot leave orphan followers. Retirement removes its catalogs and prevents late publication. ToolRegistry revision changes let Chat announce the Tool changes at the next request boundary as System Reminders while the Session's Tool list, System Prompt and messages stay unchanged until Compaction (`chat/request-building.md` -> Tool catalog per prompt epoch). Dynamic Tools also appear in the Extension capability inventory. Tests: `tests/core/extensions/test_operations.py`, `tests/core/chat/test_chat_loop_extension_tools.py`, `tests/core/chat/test_chat_loop_tool_definitions.py`, and `tests/cli/test_extensions_operations.py`.
