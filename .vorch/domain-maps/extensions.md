# Extensions

`core/extensions/` is the in-process Python extension kernel: it discovers extension identities, collects declarations, applies capabilities, dispatches hooks and interactions, and coordinates extension lifecycle with the runtime.

## Overview

An Extension is the unit of discovery, identity, configuration, enable/disable, and lifecycle. Extensions can contribute Chat hooks, Slash Commands, Tools, Recall backends, System Prompt blocks, channel interaction handlers, settings schemas, schema-described management operations, and live Tool catalogs. The owning backend domain still decides when a capability is used and what its business payload means: Chat owns hook fire-points, Command execution, and tool-result policy, Tools owns Tool execution contracts, Recall owns backend semantics, Prompts owns block assembly, Channels owns transport, and Runtime owns bootstrap/rebuild ordering.

Extensions execute arbitrary code in the vBot process on the normal asyncio runtime. They are inside the kernel trust boundary, not sandboxed plugins. Agent authoring guidance lives in the bundled `vbot-cli` Skill at `resources/skills/vbot-cli/references/extensions.md`; runnable examples live in `resources/skills/vbot-cli/assets/extensions/`.

## Terms

Core cross-cutting terms live in `.vorch/GLOSSARY.md`; these terms are specific to the Extensions domain.

### Bundled Extension

**Definition:** An Extension shipped under `resources/extensions/`, scanned after user roots and default-on unless its identity is disabled. An earlier same-name Extension shadows it.

**Not:** A separate runtime mechanism; after loading, bundled and user Extensions use the same records, declarations, and dispatch paths.

### Extension Reload

**Definition:** The serialized, restart-equivalent rebuild in `core/extensions/runtime.py::ExtensionRuntime`, exposed through `Runtime.reload_extensions()`. It picks up added, deleted, edited, fixed, and newly enabled Extensions without restarting the server process.

**Not:** Live disable, which surgically deactivates only the newly disabled loaded Extension.

### Extension Settings Schema

**Definition:** The typed field declarations an Extension registers in code to validate and render its configuration. Non-secret values live under the Extension's persisted config; secret fields point to explicit environment keys and never store or return the credential value as normal config.

## Loading and identity invariants

- `ExtensionRegistry.load()` scans immediate children in deterministic root order: `<data_dir>/extensions/`, configured `extension_directories`, then bundled `resources/extensions/` last. Failure to enumerate one root is logged and contributes no Extensions from that root; the remaining roots still load and Runtime startup continues.
- Filesystem name is Extension identity. Cross-root conflicts are first-wins: the first occurrence claims the name; later copies become visible `overridden` records and are never imported or registered. A disabled earlier copy still claims the name, so disabling cannot silently activate a later copy.
- Loading is two-phase. `register(api)` only collects declarations into an `ExtensionRecord`; successful async registrations finish deterministically before loaded records are applied in load order, while each async registration that exceeds the hard 10-second deadline becomes a failed record without blocking the remaining Extensions. Extension code never writes live registry tables directly.
- Record status is one of `loaded`, `failed`, `disabled`, or `overridden`. Import, manifest (including I/O, malformed JSON, or UTF-8 decoding), or registration failure isolates that Extension; a collision or apply error for one capability stays a non-fatal `capability_errors` diagnostic while the record remains loaded.
- `API_VERSION` is the public contract version and is currently 12; API v2 adds Slash Command declarations, API v3 adds Extension-owned Tool Family declarations, and API v4 adds management operations, host capabilities, and live Tool catalogs; API v5 adds explicit opt-in Tools; API v6 adds page declarations and explicit Tool catalog visibility; API v7 adds owner-bound Extension databases (`host.open_database`); API v8 adds Tool result payloads (`ToolContext.attach_result_payload`, `host.load_result_payload`); API v9 adds Tool-gated prompt blocks (`register_prompt_block(..., requires_tool=...)`, `extensions/capabilities.md`); API v10 adds owner group listing and archiving (`temporary_agents.groups()`, `archive_group()`); API v11 adds OAuth browser redirects (`host.oauth_redirects`, below); API v12 adds server folder listing for pages (`extensionPageClient.listDirectory`, `webui.md`). An optional directory-form `extension.json` may add display metadata and an `api_version`; a manifest requiring a newer version fails that Extension.

## Cross-task contracts

Runtime callback dispatch uses `invoke_extension_handler`: synchronous lifecycle, Hook, Command, Interaction, and Extension Tool handlers run in bounded workers, while coroutine handlers stay on the Event Loop and must yield normally. Ordinary Hook/lifecycle/interaction dispatch remains ordered and fail-open. Hook and interaction dispatch warn after at least one second wall time; this is diagnostic, not a timeout or cancellation mechanism. Successful startup and shutdown do not emit duration warnings; lifecycle exceptions remain error logs. Waits for startup, shutdown and Session-owner quiesce have a hard deadline (`invoke_lifecycle_handler`, 30 s each), so no Extension can hold startup, reload, disable or shutdown indefinitely, nor a Settings save that waits for them (`extensions/management.md` -> Startup and shutdown). Required Session runtime callbacks propagate failures and revalidate their exact registration after awaits.

- `ExtensionAPI` is the declaration facade. `api.config` is the register-time snapshot for structural choices; `api.get_config()` and `api.resolve_credential()` are live per-call reads for values that can change without rebuilding code.
- `ExtensionRegistry` is the sole owner of records, hook dispatch tables, interaction-prefix routing, capability application, diagnostics, startup/shutdown, and deactivation primitives. `CommandDispatcher` remains the sole owner of Command validation, recognition, scheduling, execution, and neutral outcomes after the registry applies declarations.
- Page declarations resolve once per registration at load; an entry outside its Extension root becomes a capability diagnostic, not a page. `current_page(identity, page_id)` answers page-scoped validation from memory: another or retired epoch, an owner no longer loaded, or an undeclared page yields `None`, and the entry file is not consulted. `page_declarations()` also requires the entry file on disk, so its callers (page listing, asset delivery) run it off the Event Loop (`tests/core/extensions/test_registration.py`, `test_runtime.py`).
- Ordinary Hook, Command, lifecycle, and interaction handlers may be synchronous or asynchronous. Their dispatch isolates failures. Session-owning Extensions instead use the explicit required Session runtime contract; failed delivery propagates to the owned Run. Completion notifications run after the durable outcome is committed; failures remain observable in logs and the terminal `completion_notification_errors` field without undoing that commit.
- Capability collisions never override an existing owner. Built-ins and earlier-loaded Extensions win; skipped capabilities are diagnosed on the affected records.
- Configuration values are live, but declaration structure is registration-bound. Config-only saves require no reload; enabling or explicit reload rebuilds the layer; disabling a loaded Extension removes its live effects and fires shutdown under the same runtime lock.
- Packaged installs may append one managed user dependency site before normal Extension discovery. It is data-dir owned and selected only when its complete runtime inventory and Python ABI fingerprint match the active release; a recipe change requires server restart. A development checkout loads Extensions without that site.
- Callback-data prefix `run` is reserved by the runtime to wake the Agent through Channels. `ExtensionRegistry` refuses Extension ownership of reserved prefixes; all other interaction prefixes remain first-wins Extension capabilities.

API 6 adds owner-bound temporary execution groups, hidden Session Tools and prompt blocks, receipt-bearing request callbacks, and isolated page declarations. `core/agents/temporary.py` owns creation/admission/drain and exact receipt/history/usage reads; it never creates Identity workspaces. Every private Tool declaration must retain its exact current registration; a missing or collided declaration blocks the Session capability. The binding's `tool_access.denied` may then remove individual private Tools from its grant set. Private denials are excluded from ordinary Tool/Project validation, while ordinary selections cannot grant private Tools. Prompt inspection and execution use the same selected grants. Their input types use the shared Tool representation normalization and validation contract; owner-specific call repairs use their argument normalizer, and public catalogs exclude them while collision detection and teardown retain them. Quiesce drains owned work before declarations are removed; a drain past its deadline is logged and left running uncancelled while removal proceeds, and the retired registration fails closed. Evidence: `tests/core/agents/test_temporary.py`, `tests/core/extensions/test_deactivate.py`, and `tests/core/chat/test_chat_loop_extension_tools.py`.

The optional host `inspect_prompt(config, project_id)` capability previews a
temporary configuration with the owner's private Tools, the selected Project's
context and Model-specific Tool routing. It returns combined text, rendered block details
and Tool definitions without creating a Session or Run. `catalog()` includes
public System Prompt block metadata. The bundled Swarm uses these capabilities
for explicit prompt composition; see `extensions/swarm.md` and
`tests/core/runtime/test_runtime_extension_host.py`.

Group titles: `temporary_agents.title_group(group_id, source_text)` stores a local
title immediately, then lets the Runtime's shared Session title generator replace it
(configured Title Model, else the approximately cheapest group participant Model by
catalog input+output rate, else the first participant; one attempt, failures keep
the local title). It awaits the Model call, so owners run it in the background.
`group_titles(group_ids)` reads stored titles within the caller's owner namespace.
Titles are canonical Session data (see `sessions.md`), so Statistics keeps showing
them after the Extension is disabled. Evidence: `test_titles.py`,
`test_runtime_extension_host.py`.

Owner-managed Session lifetime: while its Extension is installed - loaded, failed,
disabled or overridden - only the owner can remove its Sessions; ordinary archive,
move, delete and Project removal refuse them (`sessions/owned-execution.md`). API 10
lets an owner reconcile: `temporary_agents.groups(after="", limit=100)` pages its
group ids that still have live participant Sessions, and `archive_group(group_id)`
(closed groups only, else `ValueError("group_not_closed")`) archives them under a
no-Run guard as one `owner_group` archive entry (`archive.md`; listed and purgeable,
not restorable, purged by the retention sweep once the user's retention period
ends), keeping history, bindings and usage attribution; `delete_group` still
removes an archived group. Both close each live participant's Terminal Sessions first,
like an ordinary Session removal (`release_temporary_group`). Once an Extension is removed from every root, nothing
could manage its Sessions any more, so `ExtensionRuntime` archives the live ones of
every owner name without a registry record (entry reason `extension_removed`) before
startup handlers run, at initial startup and after each reload (`ExtensionHostFactory.archive_uninstalled_owner_groups`,
logged at WARNING per owner). It skips this when a scan root was missing or unreadable
(`ExtensionRegistry.installed_names()` is `None`), since absence then proves nothing,
and leaves a group with active or queued work live until the next load. Failures are
logged and never block the Extension layer. An archived participant cannot be
re-created, so a returning owner deletes such groups. Evidence:
`test_runtime_extension_host.py`, `test_runtime_extension_reload.py`.

Extension databases (API 7): an owner-bound host offers `await host.open_database(name, schema_sql, migrations=(), retired_indexes=())`, returning a kernel `Database` (`database.md`) for the canonical database `ext.<owner>.<name>` at `<data-dir>/extension-data/<owner>/<name>.db` with the shared Extension application id at format generation 1. `name` and the Extension id must be lowercase safe ids (`core.utils.ids.is_safe_id`), else `ValueError`; a second open of an already open name is refused. First open creates and registers it in the data-store marker; reopening applies the kernel's additive reconcile and named migrations, so schema changes stay additive. `core/extensions/databases.py::ExtensionDatabases`, created by `ExtensionHostFactory`, owns the handles per registration identity: `ExtensionRegistry` releases a registration after that owner's shutdown handlers in `fire_shutdown` (Runtime shutdown, reload) and `deactivate` (live disable), closing its handles; a released registration can never open again, and the reloaded Extension opens anew. `Runtime.canonical_databases()` includes the open handles, so data snapshots copy them online; registered but unopened ones are copied from their files. Offline tools (CLI data-store status/restore, updater snapshot) never declare Extension specs, because that would execute Extension code: they verify Extension members by kernel identity and integrity only, without owner facts or schema compatibility checks. The returned `Database` carries the kernel's `read_async`/`write_async`/`run_async`, so an Extension runs its blocking work on that database's own worker pool instead of a private one. Extensions import `Database`, `Migration` and `DatabaseError` from `core.extensions.databases` and never open SQLite files, `core.database` internals or `core.sessions` storage themselves. Runtime shutdown closes any handle a release missed. No uninstall flow exists: removing an Extension (deleting it from an Extension root) archives its owner-managed Sessions at the next load (above) but leaves its databases registered and their files in place, so data snapshots keep copying them; once a registered file is gone too, data snapshots and update snapshots refuse with the release command (`database.md`). `ExtensionDatabases.unregister(name)`, reached through `Runtime.unregister_extension_database`, RPC `data_store.unregister` and `vbot data-store unregister <name> --yes`, moves the files to quarantine and drops the registration; it is refused while a registration has the database open or is opening it, and an open that starts meanwhile waits, then creates a new, empty database. A disabled but installed Extension's database can be released too; its next open starts empty. Opening point: the owner-bound host is bound only when `ExtensionRuntime.startup` fires startup handlers from the server lifespan after Runtime bootstrap (and again after a reload), so the earliest open is an `api.operations.startup` handler (the Swarm Extension opens there). Safe startup modes (`--verification-only`, test instances) load no Extensions (`core/runtime/_bootstrap.py`) and fire no startup handlers, so an updater's candidate verification never opens, migrates or auto-restores an Extension database, and an Extension open failure never triggers the automatic data rollback. Startup handlers are fail-open: an open failure is logged as the handler's error and the server still starts healthy, with that Extension lacking its database. Automatic restore of a missing or corrupt Extension database happens at that open; the operator path is `vbot data-store snapshot restore <id> --database ext.<owner>.<name> --yes`, which checks only kernel identity and integrity and leaves reconcile, migrations and any refusal to the Extension's next open after the restart (`database.md` -> Conventions). Evidence: `tests/core/extensions/test_extension_databases.py`, `tests/core/runtime/test_runtime_extension_databases.py`.

Tool result payloads (API 8): an Extension Tool running in a Session keeps JSON data beside its Tool Result with `context.attach_result_payload(payload) -> payload_id` (`res_` plus 16 base32 characters; readers accept any safe opaque id, `is_safe_id`). `context.result_payloads_available` is false outside a Session (management, CLI, direct calls), where attaching raises `RuntimeError`; the Extension then returns the data inline. Chat stages the payload and stores it with the Tool Result in one transaction (`chat/run-execution.md`, `sessions.md`): a handler that raises, returns an invalid result or is cancelled leaves none, a returned failure keeps them. The owner is the attaching Tool's Extension; core Tools cannot attach. The owner-bound `await host.load_result_payload(context, payload_id)` returns the decoded payload, or `None` unless it belongs to this Extension and its Tool Result is in the calling Session's current view (own, fork-inherited, or materialized after an ancestor's deletion); it raises `ValueError` once the registration is no longer current. The Extension adds its own policy check (MCP: `extensions/mcp.md`). Source: `core/runtime/_extension_host.py`; evidence: `tests/core/runtime/test_runtime_extension_host.py`, `tests/core/chat/test_tool_result_payloads.py`, `tests/core/sessions/test_tool_result_payloads.py`.

OAuth browser redirects (API 11): `host.oauth_redirects` is the Runtime's `OAuthRedirects` (`core/extensions/oauth_redirects.py`), or `None` without a Runtime. An Extension that sends the user's browser to an authorization server uses `callback_url` as its redirect URI when it is not `None`, awaits `expect(state)` for the redirect query and calls `discard(state)` when it stops waiting. The server binds `callback_url` at startup and passes `GET /api/oauth/callback` queries to `deliver` (`server.md`); only a pending, unexpired `state` (`DEFAULT_TTL_SECONDS`, 600 s) matches, each one once, so a stray or replayed request completes nothing and cancels nothing. `callback_url` is `None` when the server listens only on an address that is not this machine's loopback or wildcard address; the Extension then needs another way back, such as the user pasting the redirected address (MCP: `extensions/mcp.md`). Evidence: `tests/core/extensions/test_oauth_redirects.py`, `tests/server/test_app_http.py`.

Every live Extension-owned Session counts in global Statistics under the reserved
actor key `extension:<owner>`, never under its synthetic participant Agent id; see
`statistics.md`.

## Ownership and source routing

- Public API, registration identity, hooks, and lifecycle: `core/extensions/extensions.py`. Internal `_declarations.py` owns declaration/record values and diagnostics; `_api.py` collects declarations; `_loading.py` owns filesystem discovery/import and registration deadlines; `_callbacks.py` owns bounded callback execution.
- `_capabilities.py` applies declared Tools, Commands, Recall backends, and Prompt blocks to their existing owners, including collision diagnosis and identity-safe Tool removal. Its installer receives the registry records, without access to the ExtensionRegistry or Runtime.
- Neutral channel-interaction types and reserved prefixes: `core/extensions/interactions.py`
- Settings field parsing and config validation: `core/extensions/settings_schema.py`
- Bootstrap construction and cross-domain callback wiring: `core/runtime/runtime.py`; serialized rebuild, disable, registry swap, and lifecycle ordering: `core/extensions/runtime.py`
- Management and secret RPC projection: `server/rpc/extensions_methods.py`, with disabled/config persistence in `server/rpc/settings_methods.py` and the Settings domain
- Bundled implementations: `resources/extensions/`; examples: `resources/skills/vbot-cli/assets/extensions/`

## Constraints and gotchas

- Never treat Extensions as untrusted sandboxed code or expose their directories as remote-install targets. Loading Python is code execution with process privileges.
- Do not add a new hook by accepting arbitrary event names alone. The registry's typed dispatch method and the owning domain's fire-point/payload contract must exist together.
- `vbot_ext` and its submodules are purged during full reload so edited package submodules are re-imported. A partial module swap would leave stale code and is not the supported reload model.
- Disabling clears a record's declarations after removing hooks, interaction handlers, applied Tools and Commands, and running shutdown. Re-enabling therefore requires the full load path.
- An Extension database handle dies with its registration: shutdown handlers may still use it, but code that keeps running after shutdown (background tasks, cached handles in module globals) gets a closed handle. Open again from the next startup's owner host instead of caching across reloads.
- Secret clients submit the schema field key, never an arbitrary environment key. The server resolves the declared `env_key`, writes or removes the data-dir credential, reloads credential state, and returns only whether it is set.

## Agent-facing text

Rows cover only the bundled-page paragraphs of the `vbot-cli` Skill's page reference (`resources/skills/vbot-cli/references/extension-pages.md`) listed below; its older wording has no recorded reasons yet.

| Text | Reason |
|---|---|
| `A bundled page keeps its English UI text in its own catalog: ...` | The WebUI English catalog is the only source of page text and `t()` takes no English fallback (`webui.md`). Without the paragraph, an Agent authoring a bundled page hard-codes English text or calls `t()` for keys no catalog holds, which render as raw keys; a key that collides with the WebUI catalog makes `registerCatalog` throw before the page mounts. Preventive; no Session evidence yet (2026-09). |
| `The context's route is the page's place in the app's Back/Forward history: ...` | Pages became places in the app-wide Back/Forward history (2026-09-29, `webui/app-shell.md` -> Navigation): `pushRoute` adds a step, `replaceRoute` corrects the current one, and the route arrives through context. Without it, an Agent authoring a page replaces the route on every selection, so Back skips the page's steps, or applies its own selection instead of the route it receives, so Back does nothing visible. Preventive; no Session evidence yet. |
| `A Svelte page calls provideNavigation(...) ...` | Back and Forward close the topmost dialog or menu app-wide before navigating (2026-09-29). A page that does not provide the client's layer registry leaves its dialogs open while Back moves the page's place underneath them. Preventive; no Session evidence yet. |
| `An invalidation callback receives {reason, change}: ...` | Scoped page invalidation (2026-09-28): `publish_change` forwards `{resource, ids, revision}` only to the owning Extension's open page, and `null` marks a full refresh (descriptor reload, reconnect, Run-stream recovery). Without the sentence, an Agent authoring a page reloads every read model on each change; the Swarm page did exactly that under a 23-Agent load (~3.5 operations/s plus a descriptor reload per change). |
| `To let the user enter a path on the vBot server, ... render the shared PathField ...` | Server paths are entered only through the shared `PathField`, never a browser file dialog, because the server may run on another computer (`webui.md` -> Ownership); pages can list the server's folders since API v12 (2026-10, `client.listDirectory`). Without the paragraph, an Agent authoring a page offers a browser file input, which reads the viewer's computer and yields no server path, or a plain text field without completion; `projectShortcuts` would call the app API, which the sandboxed page cannot reach. The trailing-separator sentence comes from the Swarm page: a completed `C:/work/` differed from the profile's `C:/work` and counted as a changed Run directory. The field drops the separator when focus leaves it (2026-10), but a value submitted while the field has focus still carries it. Preventive; no Session evidence yet. |

## References

Read these only when your task matches - not by default.

- Authoring an Extension or adapting a runnable template -> `resources/skills/vbot-cli/references/extensions.md` (pages and owned Sessions: `references/extension-pages.md`) and `resources/skills/vbot-cli/assets/extensions/` (repository-relative); bundled usage guidance -> `resources/skills/vbot-cli/references/extension-usage.md`
- Adding or changing hooks, Command/Tool/Recall/Prompt capabilities, channel interactions, dispatch decisions, collision behavior, or handler payloads -> `extensions/capabilities.md`
- Changing discovery, manifests, records, settings schemas, secret handling, visibility, enable/disable, startup/shutdown, or full reload -> `extensions/management.md`
- Changing Computer Use, its opt-in Tools, the access mode and app approvals, screenshots and coordinates, the desktop target, or desktop input -> `extensions/computer-use.md`
- Restoring the archived Browser Use Extension or locating its replacement -> `extensions/browser-use.md`
- Changing the bundled Home Assistant Extension, its four Tools, settings, readiness, retry behavior, or security constraints -> `extensions/homeassistant.md`
- Changing the bundled MCP client, transports, Tool opt-ins, callbacks, media preservation, or protocol compatibility -> `extensions/mcp.md`
- Adding or changing an Extension-owned page or configuration editor -> `webui.md`; editable settings, existing-record forms, and save/navigation behavior also require `webui/autosave.md`
- Changing Swarm profiles, peer coordination, participant delivery/completion, or the Swarms page -> `extensions/swarm.md`
- Informing the Model about a background event or state change -> `model-communication.md`
