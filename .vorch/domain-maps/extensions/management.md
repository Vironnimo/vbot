# Extension Management

Read this reference only for Extension discovery, manifests, records, settings schemas, secrets, visibility, lifecycle, enable/disable, or full reload work. Capability semantics live in `extensions/capabilities.md`.

## Discoverable shapes and manifests

Each scan root accepts immediate `.py` files and directories with an Extension entry point. Import names live under the synthetic `vbot_ext` package so package-relative imports work. Identity is the file stem or directory name; the optional directory manifest enriches display metadata but never replaces filesystem identity.

`settings.extension_directories` is the only configurable extra-root list; the bundled root is fixed separately and cannot be removed through that setting. Runtime ignores a non-list value with a warning, skips non-string or empty entries, expands `~`, and preserves configured order before the final bundled root.

`ExtensionRecord` retains every discovered result in load order: identity and paths, `status`, load `error`, optional manifest, collected declarations, non-fatal `capability_errors`, and `overridden_by` for shadowed copies. `records()` includes all statuses; `diagnostics()` includes only failed records.

The load path must preserve first-wins root shadowing before import. Overridden copies are not imported and their manifests are not read. Disabled winners are recorded without import but still shadow later roots.

## Settings schemas and live values

`api.register_settings(fields)` declares at most one schema. `settings_schema.py` parses field keys, labels/descriptions, required/default metadata, and the supported `text`, `number`, `toggle`, and `secret` types. Invalid declarations fail registration with a field-specific error.

Non-secret values live in `settings.extensions.config.<extension>` and are read live through `api.get_config()`. The Settings RPC layer validates config for loaded schema-owning Extensions before persistence because that layer has both the registry and the update; core Settings storage continues to own only the generic section shape.

A secret declaration supplies an explicit `env_key`, cannot carry a normal default, and never enters the JSON config. `extensions.set_secret` accepts Extension name, schema field key, and a string value; it resolves the declared field server-side, writes or clears the data-dir credential, reloads Provider credentials, and returns only `{name, key, set}`. Process-environment precedence still applies to live resolution.

## Visibility contract

`extensions.list` and `extensions.reload` return the same `{extensions: [...]}` catalog shape. Each record projection includes status/path/error/shadowing/manifest metadata, persisted non-secret config, loaded schema metadata, capability summary, and derived readiness; secret fields expose only their `env_key` and live `set` boolean. Declared Commands are projected with their name and whether the stable live `CommandDispatcher` currently registers that name to this Extension, so collision-skipped declarations remain visible without being reported as active.

Readiness is a display projection, not a stored Extension status. A loaded Extension with declared Tools is `waiting` when at least one currently applied Tool is not ready; otherwise it is `ready`. A collision-skipped Tool is not applied and therefore not ready.

## Enable, disable, and configuration decisions

The disabled set and config are persisted together under Settings, but their runtime consequences differ:

- Config-only change: persist and return; Extension handlers read new values through `get_config()` with no rebuild.
- Disable-only change: under `_extension_reload_lock`, deactivate newly disabled loaded records, remove their applied Commands, refresh Prompt blocks and Extension-bundled Skills, and recover the active Recall backend if necessary.
- Any newly enabled name: perform one full `reload_extensions()` after persistence. A mixed enable/disable save needs no second disable pass because the rebuild reads the final persisted set.
- Explicit reload: rebuild the whole layer even when the disabled set did not change, so disk edits/additions/deletions and repaired failures become visible.

Successful explicit reload and Settings mutations that reload/enable/disable Extensions publish `resource_changed(kind="commands")` after the structural work. Config-only saves do not publish it because per-call values change without altering the Command catalog.

## Full reload sequence

`Runtime.reload_extensions()` runs on the serving loop under `_extension_reload_lock`: read fresh roots/settings; detach old Extension Tools and Commands; await old shutdown; purge all `vbot_ext` modules; load a fresh registry; swap it in; reapply Tools and Commands to their stable owners; rebuild Recall, Prompt blocks, and Skills against the new loaded set; then await new startup.

The rebuild is restart-equivalent for the Extension layer, not atomic Run draining. A concurrently executing handler may finish against old code; normal per-handler fail-open isolation is the accepted boundary. Prepared Extension Commands carry a registration identity, so deferred Channel work that has not started execution cannot invoke removed code or a replacement owner.

## Startup and shutdown

Loaded Extensions fire startup in load order after runtime capability application. Runtime stop fires shutdown for loaded records; live reload awaits old shutdown and new startup on the serving loop; live disable fires only that record's shutdown. Synchronous and asynchronous lifecycle handlers share fail-open logging and do not prevent remaining handlers from running.

## Source and tests

- Discovery, records, import, manifest, schema declaration, lifecycle: `core/extensions/extensions.py`, `core/extensions/settings_schema.py`
- Runtime mutation: `core/runtime/runtime.py`
- RPC catalog/secrets and Settings delta: `server/rpc/extensions_methods.py`, `server/rpc/settings_methods.py`
- Focused coverage: `tests/core/extensions/test_loader.py`, `test_registration.py`, `test_settings_schema.py`, `test_reload_primitives.py`, `test_deactivate.py`, and Extension RPC/Runtime tests
