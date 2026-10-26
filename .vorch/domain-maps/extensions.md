# Extensions

`core/extensions/` is the in-process Python extension kernel: it discovers extension identities, collects declarations, applies capabilities, dispatches hooks and interactions, and coordinates extension lifecycle with the runtime.

## Overview

An Extension is the unit of discovery, identity, configuration, enable/disable, and lifecycle. Extensions can contribute Chat hooks, Slash Commands, Tools, Recall backends, System Prompt blocks, channel interaction handlers, settings schemas, schema-described management operations, and live Tool catalogs. The owning backend domain still decides when a capability is used and what its business payload means: Chat owns hook fire-points, Command execution, and tool-result policy, Tools owns Tool execution contracts, Recall owns backend semantics, Prompts owns block assembly, Channels owns transport, and Runtime owns bootstrap/rebuild ordering.

Extensions execute arbitrary code in the vBot process on the normal asyncio runtime. They are inside the kernel trust boundary, not sandboxed plugins. Agent authoring guidance lives in the bundled `vbot-docs` Skill at `resources/skills/vbot-docs/references/extensions.md`; runnable examples live in `resources/skills/vbot-docs/assets/extensions/`.

The bundled `vbot-docs` Extension references each fit one `skill` Tool read (`tests/resources/skills/test_bundled_skills.py` reads each whole). Its `assets/extensions/` templates are recursively readable Skill resources, not Extension scan roots: the nested `workflow` Skill becomes separately discoverable only after its containing Extension is installed and loaded (`tests/core/extensions/test_examples.py`).

## Terms

Shared terms (Tool, Skill, Session) live in `.vorch/GLOSSARY.md`.

### Bundled Extension
An Extension that ships with vBot in its install tree; it loads after the user's Extensions, so a same-name user Extension shadows it (Loading and identity invariants). Not a separate runtime mechanism: once loaded, bundled and user Extensions use the same records, declarations and dispatch paths.

### Extension Reload
The serialized, restart-equivalent rebuild of the Extension layer that picks up added, deleted, edited, fixed and newly enabled Extensions without restarting the server process (`extensions/management.md` -> Full reload sequence). Not live disable, which deactivates only the newly disabled loaded Extension.

### Extension Settings Schema
The typed field declarations an Extension registers in code to validate and render its configuration (`extensions/management.md` -> Settings schemas and live values). Not a credential store: a secret field names an environment key, and its value is never stored or returned as normal config.

## Loading and identity invariants

- `ExtensionRegistry.load()` scans immediate children in deterministic root order: `<data_dir>/extensions/`, configured `extension_directories`, then bundled `resources/extensions/` last. Failure to enumerate one root is logged and contributes no Extensions from that root; the remaining roots still load and Runtime startup continues.
- Filesystem name is Extension identity. Cross-root conflicts are first-wins: the first occurrence claims the name; later copies become visible `overridden` records and are never imported or registered. A disabled earlier copy still claims the name, so disabling cannot silently activate a later copy. An individually unreadable candidate or entry point keeps a failed record and its owner identity, protecting its Sessions from removal cleanup and blocking later copies. When a `.py` entry's kind cannot be checked, both possible identities (file stem and directory name) stay reserved until it becomes readable (`test_loader.py`, `test_runtime_extension_reload.py`).
- Loading is two-phase. `register(api)` only collects declarations into an `ExtensionRecord`; `aload()` runs discovery, imports, synchronous registration and page-path resolution on a bounded loader worker, while async registration and declaration publication stay on the serving Event Loop (`extensions/management.md`). Successful async registrations finish deterministically before loaded records are applied in load order; a registration past its hard 10-second deadline fails only that Extension. Extension code never writes live registry tables directly.
- Record status is one of `loaded`, `failed`, `disabled`, or `overridden`. Import, manifest (including I/O, malformed JSON, or UTF-8 decoding), or registration failure isolates that Extension; a collision or apply error for one capability stays a non-fatal `capability_errors` diagnostic while the record remains loaded.
- `API_VERSION` (`core/extensions/_declarations.py`) is the public contract version, currently 21. Bump it when the Extension contract changes in a way third-party Extensions can detect through their manifest `api_version`, and record what the new version adds in `extensions/management.md` -> API versions. An optional directory-form `extension.json` may add display metadata, an `api_version` and `config_documents` validators (`extensions/management.md` -> Offline document validation); a manifest requiring a newer version fails that Extension.

## Cross-task contracts

The package facade loads public exports on first access and caches their identity; lightweight imports such as OAuth redirects do not load the registry, discovery or management stack.

Callback execution and fail-open lifecycle/Hook policy live in `_callbacks.py`; `_loading.py` resolves page entry paths and `_capabilities.py` installs interaction-prefix ownership. Runtime callback dispatch uses owner-bound `invoke_extension_handler`: synchronous callbacks share bounded workers with per-owner admission that reserves capacity for other Extensions; an owner with unfinished detached work rejects new callbacks until that work settles (`extensions/capabilities.md`). Coroutine handlers stay on the Event Loop and must yield normally; Extension Tools use the Tools worker boundary. Ordinary Hook/lifecycle/interaction dispatch remains ordered and fail-open. Hook and interaction dispatch warn after at least one second wall time; this is diagnostic, not a timeout or cancellation mechanism. Successful startup and shutdown do not emit duration warnings; lifecycle exceptions remain error logs. Waits for startup, shutdown and Session-owner quiesce have a hard deadline (`invoke_lifecycle_handler`, 30 s each), so no Extension can hold startup, reload, disable or shutdown indefinitely, nor a Settings save that waits for them (`extensions/management.md` -> Startup and shutdown). Required Session runtime callbacks propagate failures and revalidate their exact registration after awaits.

- `ExtensionAPI` is the declaration facade. `api.config` is the register-time snapshot for structural choices; `api.get_config()` and `api.resolve_credential()` are live per-call reads for values that can change without rebuilding code.
- `ExtensionRegistry` is the sole owner of records, hook dispatch tables, interaction-prefix routing, capability application, diagnostics, startup/shutdown, and deactivation primitives. `CommandDispatcher` remains the sole owner of Command validation, recognition, scheduling, execution, and neutral outcomes after the registry applies declarations.
- Ordinary Hook, Command, lifecycle, and interaction handlers may be synchronous or asynchronous. Their dispatch isolates failures. Session-owning Extensions instead use the explicit required Session runtime contract; failed delivery propagates to the owned Run. Completion notifications run after the durable outcome is committed; failures remain observable in logs and the terminal `completion_notification_errors` field without undoing that commit.
- Capability collisions never override an existing owner. Built-ins and earlier-loaded Extensions win; skipped capabilities are diagnosed on the affected records.
- Configuration values are live, but declaration structure is registration-bound. Config-only saves require no reload; enabling or explicit reload rebuilds the layer; disabling a loaded Extension removes its live effects and fires shutdown under the same runtime lock.
- Packaged installs may append one managed user dependency site before discovery; a recipe change needs a server restart, not an Extension Reload (`extensions/management.md` -> Startup and shutdown).
- Callback-data prefixes `run` (wakes the Agent through Channels) and `chk` (Channel checklist items) are reserved by the runtime. `ExtensionRegistry` refuses Extension ownership of reserved prefixes; all other interaction prefixes remain first-wins Extension capabilities.

## Owner-bound host

Each loaded Extension reaches Runtime services only through its owner-bound host and the owned-execution capabilities (`extensions/host.md`; owned-Session storage and authority: `sessions/owned-execution.md`). Invariants every Extension change must keep:

- Private Session Tools of a temporary execution group must keep their exact current registration; a missing or collided declaration blocks the Session capability, ordinary selections never grant them, and public catalogs exclude them.
- While its Extension is installed (loaded, failed, disabled or overridden), only the owner removes its Sessions. Owners no longer installed in any root get their live Sessions archived at the next load, before startup handlers run.
- Owner-scoped capabilities (group titles, Tool result payloads, deferred-call receipts, databases) stay within the calling owner, and a retired registration's host capabilities fail closed.
- Deferred Tool outcomes are delivered once per receipt through Sessions; recovery never restarts a Run or repeats a mutation.
- Tool result payloads attach only inside a Session; elsewhere the Extension returns the data inline. An Extension reads back only its own; built-in Tools own theirs as `tool:<name>`.
- API 21 exposes the shared Computer registry as `host.computers` for Session-selected native capability routing; capability owners resolve the calling context before execution (`computers.md`, `extensions/host.md`).

## Extension databases

An Extension opens its databases only through `await host.open_database(name, schema_sql, ...)`: each is the canonical kernel database `ext.<owner>.<name>` under `<data-dir>/extension-data/<owner>/`, with additive schema changes and handles that die with the registration. Extensions never open SQLite files, `core.database` internals or `core.sessions` storage themselves. Opening point, snapshots, restore and unregister: `extensions/databases.md`; kernel side: `database.md`.

## Constraints and gotchas

- Never treat Extensions as untrusted sandboxed code or expose their directories as remote-install targets. Loading Python is code execution with process privileges.
- Do not add a new hook by accepting arbitrary event names alone. The registry's typed dispatch method and the owning domain's fire-point/payload contract must exist together.
- `vbot_ext` and its submodules are purged during full reload so edited package submodules are re-imported. A partial module swap would leave stale code and is not the supported reload model.
- Disabling clears a record's declarations after removing hooks, interaction handlers, applied Tools and Commands, and running shutdown. Re-enabling therefore requires the full load path.
- An Extension database handle dies with its registration: shutdown handlers may still use it, but code that keeps running after shutdown (background tasks, cached handles in module globals) gets a closed handle. Open again from the next startup's owner host instead of caching across reloads.
- Secret clients submit the schema field key, never an arbitrary environment key. The server resolves the declared `env_key`, writes or removes the data-dir credential, reloads credential state, and returns only whether it is set.

## Agent-facing text

This domain owns the bundled-page paragraphs of the `vbot-docs` Skill's page reference (`resources/skills/vbot-docs/references/extension-pages.md`). Before you change, add or delete a sentence of them, read `extensions/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Authoring an Extension or adapting a runnable template -> `resources/skills/vbot-docs/references/extensions.md` (pages and owned Sessions: `resources/skills/vbot-docs/references/extension-pages.md`) and `resources/skills/vbot-docs/assets/extensions/` (repository-relative); bundled usage guidance -> `resources/skills/vbot-docs/references/extension-usage.md`
- Adding or changing hooks, Command/Tool/Recall/Prompt capabilities, page declarations, channel interactions, dispatch decisions, collision behavior, or handler payloads -> `extensions/capabilities.md`
- Changing discovery, manifests, `config_documents` validators, records, settings schemas, secret handling, visibility, enable/disable, startup/shutdown, the managed dependency site, or full reload, or bumping `API_VERSION` -> `extensions/management.md`
- Changing an owner-bound host capability: temporary execution groups and private Session Tools, prompt inspection, group titles, owner-managed Session removal and archiving, deferred Tool outcomes, Tool result payloads, OAuth browser redirects or Computer routing -> `extensions/host.md`
- Changing how an Extension opens, migrates, snapshots, restores or unregisters its databases -> `extensions/databases.md`
- Changing Computer Use, its opt-in Tools, the access mode and app approvals, screenshots and coordinates, the desktop target, or desktop input -> `extensions/computer-use.md`
- Routing an Extension's native capability to the Session's Computer -> `computers.md`
- Restoring the archived Browser Use Extension or locating its replacement -> `extensions/browser-use.md`
- Changing the bundled Home Assistant Extension, its four Tools, settings, readiness, retry behavior, or security constraints -> `extensions/homeassistant.md`
- Changing the bundled KeePassXC Secret Source, its settings, unlock, keepassxc-cli calls, entry paths, field mapping, or entry creation -> `extensions/keepassxc.md`
- Changing the bundled 1Password Secret Source, its service account token handling, op calls, vault and item mapping, error classification, or rate-limit caching -> `extensions/onepassword.md`
- Changing the bundled MCP client, transports, Tool opt-ins, callbacks, media preservation, or protocol compatibility -> `extensions/mcp.md`
- Adding or changing an Extension-owned page or configuration editor -> `webui.md` and `webui/extension-pages.md`; editable settings, existing-record forms, and save/navigation behavior also require `webui/autosave.md`
- Changing Swarm profiles, peer coordination, participant delivery/completion, or the Swarms page -> `extensions/swarm.md`
- Informing the Model about a background event or state change -> `model-communication.md`
