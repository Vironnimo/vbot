# Settings, Prompts, Extensions

## Raw settings

```bash
vbot config                        # show stored raw settings
vbot config effective              # show normalized live settings
vbot config get <key>
vbot config set <key> <value>
```

- `effective` includes normalized defaults and runtime-derived availability. A successful `config set` prints the value returned from the saved settings, not merely the requested input.
- `<value>` is parsed as JSON first, falling back to a plain string. Pass JSON (arrays, objects, booleans, numbers) as one shell argument: `vbot config set skill_directories '["C:/skills"]'`.
- Prefer `config set` over editing `settings.json` directly. After an unavoidable manual edit, run `vbot doctor settings` (or `vbot doctor config` for the full user-editable JSON bundle) — see `server.md`.
- Enable debug mode with `vbot config set debug '{"enabled": true}'`.

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
