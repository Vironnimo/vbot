# Settings, Prompts, Extensions

## Settings

```bash
vbot config                                      # list the public Settings catalog
vbot config list [<path-prefix>]                 # filter catalog paths, values, types, lifecycle, and source
vbot config describe <path>                      # show type, constraints, default, source, and lifecycle
vbot config get <path> [--details]               # show the effective value, optionally with metadata
vbot config set <path> <value>                    # atomically set one path
vbot config unset <path>                          # remove one override and restore inherited/default behavior
vbot config patch --set <path> <value> ...        # apply every repeated --set/--unset as one atomic change
vbot config effective                             # show the complete normalized public Settings document
vbot config raw                                   # diagnostic-only internal settings.json document
```

- Use cataloged public paths, never raw storage-key names. Fixed segments are dotted (`web_search.searxng.base_url`); user-controlled map keys are bracketed JSON strings (`'local_models.context_windows["ollama/qwen2.5:7b"]'`, `'providers.connections["openai:api-key"]'`). Quote the whole path whenever it contains brackets so the shell passes it unchanged.
- `<value>` is parsed as JSON first, falling back to a plain string. Numbers, booleans, arrays, objects, and `null` therefore use JSON syntax; pass arrays and objects as one shell argument, for example `vbot config set skills.directories '["C:/skills"]'`.
- `unset` is not the same as setting `null`: `unset` removes the configured override and restores the default/inherited value, while `null` is an explicit value accepted only by nullable paths. `describe` reports whether a path is nullable and unsettable.
- `patch` is the right command when fields must change together. Every operation is validated first; an invalid value, unknown path, duplicate path, or parent/child overlap persists nothing. A successful mutation reports each active value, any pending next-start value, whether the setting applies live or on restart, and an aggregate `restart_required` result.
- `effective` includes normalized defaults and active runtime values. `raw` exposes the internal persistence shape only for diagnosis; do not derive paths from it and do not edit `settings.json` when a public command can express the change. After an unavoidable manual edit, run `vbot doctor settings` (or `vbot doctor config` for the full user-editable JSON bundle) — see `server.md`.
- Secrets are not Settings paths. Use `provider set-key`, `extensions <name> set <field> --stdin`, or the Channel token commands so credentials never enter `settings.json` or normal command output.

Switch `web_search` to SearXNG in one atomic live change:

```bash
vbot config patch \
  --set web_search.provider searxng \
  --set web_search.searxng.base_url https://searxng.example/
vbot config get web_search.provider --details
vbot config get web_search.searxng.base_url --details
```

Both paths are read by the `web_search` Tool for each call, so this change needs no server restart.

Other examples:

```bash
vbot config set debug.enabled true
vbot config unset defaults.agent.temperature
vbot config patch --set compaction.trigger.type input_tokens --set compaction.trigger.tokens 120000
vbot config set 'local_models.context_windows["ollama/qwen2.5:7b"]' 32768
```

## System Prompt blocks

```bash
vbot prompt list
vbot prompt update <block-id> (--content <text> | --file <path>)
vbot prompt reset <block-id>
vbot prompt create <slug> [--content <text> | --file <path>] [--position <index>]
vbot prompt remove <user:block-id>
vbot prompt set-layout --layout-json <json-array>
vbot prompt reset-layout
vbot prompt preview <agent>
```

- Every command accepts `--scope default|agent:<id>` (default: `default`). An Agent scope exists only for a known Identity Agent with custom System Prompt enabled; it does not target a Project Agent.
- `list` shows one row per block: id, owner, kind, enabled, editable, source, modified, plus the available scopes. `update`/`reset` target a block by id (e.g. `core:tools`) and work only on editable blocks.
- `create` creates `user:<slug>` and optionally inserts it at a 0-based layout position. `remove` deletes only a custom `user:` block. `set-layout` takes the complete ordered `[ {"id": "...", "enabled": true} ]` array as one shell argument; `reset-layout` restores the bundled order and enabled states without resetting text overrides.
- Prefer `--file` for multi-line content. Do not edit block override files directly when these commands can express the change.
- `preview` renders one Agent's complete System Prompt with text-token and Tool-definition token metadata; its Agent positional accepts `agent@projekt` unless an Identity Agent scope is explicitly selected.

## Extensions

```bash
vbot extensions list
vbot extensions reload
vbot extensions <name>                        # show settings: fields, values, secret set-state
vbot extensions <name> set <field> <value>
vbot extensions <name> set <field> --stdin    # read the value from stdin (secrets)
vbot extensions enable|disable <name>
```

Every extension command applies live — no restart:

- `set` routes by the field's declared type: a secret field is stored in the data-dir `.env` under the key the extension declares; any other field is type-validated and written to the extension's live config. `<field>` is the schema field key (e.g. `token`), not an env-variable name — inspect with `vbot extensions <name>` first.
- For secrets, prefer `--stdin` to keep the value out of shell history, and never echo it back. An empty value clears a secret.
- `enable` rebuilds the extension layer so freshly-loaded code takes effect at once (the command warns when the enabled extension still failed to load); `disable` deactivates the extension immediately.
- `reload` rebuilds the whole extension layer from disk — use it after editing extension code or adding/removing extension directories.
- `list` shows loaded/failed/overridden/disabled extensions with their capabilities, and for a loaded-but-unconfigured extension what it is waiting for.

Typical setup flow when the user hands you an extension secret ("here is my Home Assistant token"):

```bash
vbot extensions homeassistant                                   # see the fields; it is waiting for a token
vbot extensions homeassistant set url http://homeassistant.local:8123
vbot extensions homeassistant set token --stdin
```
