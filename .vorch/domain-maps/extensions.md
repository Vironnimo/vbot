# Extensions

`core/extensions/` is the in-process Python extension kernel: it discovers extension identities, collects declarations, applies capabilities, dispatches hooks and interactions, and coordinates extension lifecycle with the runtime.

## Overview

An Extension is the unit of discovery, identity, configuration, enable/disable, and lifecycle. Extensions can contribute Chat hooks, Tools, Recall backends, System Prompt blocks, channel interaction handlers, and settings schemas. The owning backend domain still decides when a capability is used and what its business payload means: Chat owns hook fire-points and tool-result policy, Tools owns Tool execution contracts, Recall owns backend semantics, Prompts owns block assembly, Channels owns transport, and Runtime owns bootstrap/rebuild ordering.

Extensions execute arbitrary code in the vBot process on the normal asyncio runtime. They are inside the kernel trust boundary, not sandboxed plugins. User-facing author guidance lives in `docs/extensions.md`; runnable examples live in `examples/extensions/`.

## Terms

Core cross-cutting terms live in `.vorch/GLOSSARY.md`; these terms are specific to the Extensions domain.

### Bundled Extension

**Definition:** An Extension shipped under `resources/extensions/`, scanned after user roots and default-on unless its identity is disabled. An earlier same-name Extension shadows it.

**Not:** A separate runtime mechanism; after loading, bundled and user Extensions use the same records, declarations, and dispatch paths.

### Extension Reload

**Definition:** `Runtime.reload_extensions()`, the serialized, restart-equivalent rebuild of the whole Extension layer from current disk and persisted settings. It picks up added, deleted, edited, fixed, and newly enabled Extensions without restarting the server process.

**Not:** Live disable, which surgically deactivates only the newly disabled loaded Extension.

### Extension Settings Schema

**Definition:** The typed field declarations an Extension registers in code to validate and render its configuration. Non-secret values live under the Extension's persisted config; secret fields point to explicit environment keys and never store or return the credential value as normal config.

## Loading and identity invariants

- `ExtensionRegistry.load()` scans immediate children in deterministic root order: `<data_dir>/extensions/`, configured `extension_directories`, then bundled `resources/extensions/` last.
- Filesystem name is Extension identity. Cross-root conflicts are first-wins: the first occurrence claims the name; later copies become visible `overridden` records and are never imported or registered. A disabled earlier copy still claims the name, so disabling cannot silently activate a later copy.
- Loading is two-phase. `register(api)` only collects declarations into an `ExtensionRecord`; async registrations finish deterministically before loaded records are applied in load order. Extension code never writes live registry tables directly.
- Record status is one of `loaded`, `failed`, `disabled`, or `overridden`. Import, manifest, or registration failure isolates that Extension; a collision or apply error for one capability stays a non-fatal `capability_errors` diagnostic while the record remains loaded.
- `API_VERSION` is the public contract version. An optional directory-form `extension.json` may add display metadata and an `api_version`; a manifest requiring a newer version fails that Extension.

## Cross-task contracts

- `ExtensionAPI` is the declaration facade. `api.config` is the register-time snapshot for structural choices; `api.get_config()` and `api.resolve_credential()` are live per-call reads for values that can change without rebuilding code.
- `ExtensionRegistry` is the sole owner of records, hook dispatch tables, interaction-prefix routing, capability application, diagnostics, startup/shutdown, and deactivation primitives.
- Hook, lifecycle, and interaction handlers may be synchronous or asynchronous. Runtime dispatch isolates failures and continues; Extensions must not be able to take down the Run or the remaining handlers merely by raising.
- Capability collisions never override an existing owner. Built-ins and earlier-loaded Extensions win; skipped capabilities are diagnosed on the affected records.
- Configuration values are live, but declaration structure is registration-bound. Config-only saves require no reload; enabling or explicit reload rebuilds the layer; disabling a loaded Extension removes its live effects and fires shutdown under the same runtime lock.
- Callback-data prefix `run` is reserved by the runtime to wake the Agent through Channels. `ExtensionRegistry` refuses Extension ownership of reserved prefixes; all other interaction prefixes remain first-wins Extension capabilities.

## Ownership and source routing

- Public declarations, records, loader, capability application, hooks, and lifecycle: `core/extensions/extensions.py`
- Neutral channel-interaction types and reserved prefixes: `core/extensions/interactions.py`
- Settings field parsing and config validation: `core/extensions/settings_schema.py`
- Runtime bootstrap, rebuild, disable, Prompt/Recall/Skill refresh: `core/runtime/runtime.py`
- Management and secret RPC projection: `server/rpc/extensions_methods.py`, with disabled/config persistence in `server/rpc/settings_methods.py` and the Settings domain
- Bundled implementations: `resources/extensions/`; examples: `examples/extensions/`

## Constraints and gotchas

- Never treat Extensions as untrusted sandboxed code or expose their directories as remote-install targets. Loading Python is code execution with process privileges.
- Do not add a new hook by accepting arbitrary event names alone. The registry's typed dispatch method and the owning domain's fire-point/payload contract must exist together.
- `vbot_ext` and its submodules are purged during full reload so edited package submodules are re-imported. A partial module swap would leave stale code and is not the supported reload model.
- Disabling clears a record's declarations after removing hooks, interaction handlers, applied Tools, and running shutdown. Re-enabling therefore requires the full load path.
- Secret clients submit the schema field key, never an arbitrary environment key. The server resolves the declared `env_key`, writes or removes the data-dir credential, reloads credential state, and returns only whether it is set.

## References

Read these only when your task matches — not by default.

- Adding or changing hooks, Tool/Recall/Prompt capabilities, channel interactions, dispatch decisions, collision behavior, or handler payloads → `extensions/capabilities.md`
- Changing discovery, manifests, records, settings schemas, secret handling, visibility, enable/disable, startup/shutdown, or full reload → `extensions/management.md`
- Changing the bundled Home Assistant Extension, its four Tools, settings, readiness, retry behavior, or security constraints → `extensions/homeassistant.md`
