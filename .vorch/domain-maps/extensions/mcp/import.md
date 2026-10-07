# MCP setup import

Read for work on the MCP `import` management operation, `resources/extensions/mcp/_importer.py`, the WebUI Import dialog or the editor's quick fill. Connection ownership, saving and credentials stay as described in `extensions/mcp.md`.

## Parsing (`_importer.parse_setup`)

`parse_setup(text, ids)` turns setup text written for other MCP clients into `ImportDraft`s. It is pure: it reads no saved connection and no credential, so a clash with a saved connection appears only as the preview's `conflict`. Text over `MAX_SETUP_CHARACTERS` (200000) or empty text is refused; a leading BOM is ignored.

Recognized input:
- TOML when a `[mcp_servers` table appears (Codex `config.toml`).
- JSON, or JSONC (comments, trailing commas), starting with `{`, `[` or `"`; a fragment such as `"name": {...},` copied out of a larger file is read as an object. Server maps under `mcpServers` (Claude Desktop, Cursor, Claude Code, Windsurf, Gemini CLI, Cline), `servers` with VS Code `inputs` (also nested under `mcp` in VS Code user settings), `context_servers` (Zed, including its earlier `command: {path, args, env}` form) or `mcp_servers`; a single server object; an object whose values are all server objects; an array of server objects with `name`.
- Any other text: one entry per line (`\`, `^` and backtick continuations joined, shell prompts `$ `, `> `, `PS ...> ` stripped, `#` lines skipped). A line is `claude mcp add` or `gemini mcp add` (`-t/--transport`, `-e/--env`, `-H/--header` read; other options reported as ignored), `claude mcp add-json <name> <json>`, `code --add-mcp <json>`, a single URL, or a command line with leading `NAME=value` assignments, optionally after `env`. `split_command_line` keeps Windows paths: a backslash escapes only a quote or whitespace.

Fields: `type`/`transport` (`stdio`/`local`, `http`/`streamable-http` spellings, `sse`; anything else is a draft `error`); without one a command means stdio, and a URL whose path ends in `/sse` means legacy SSE, else Streamable HTTP. `url`/`serverUrl`/`httpUrl`; `headers`/`http_headers`, Codex `bearer_token_env_var` (an `Authorization: Bearer ${env:NAME}` header) and `env_http_headers` (header references); `disabled: true` or `enabled: false` mark the draft `disabled`; `description` (whitespace collapsed, cut at 200 characters); `tool_timeout_sec` becomes `timeout` when within the connection limit; `cwd` is kept only when absolute and free of placeholders. Other fields are reported, never guessed.

Names and ids: the entry's name, else one derived from the launched package, image, module or script, or from the URL host (common affixes such as `mcp-server-` and `-mcp` dropped), made unique within the text. The connection id is `connection_id(name)` (lowercase ASCII, digits, underscores, at most 32 characters), unique among the drafts; `ids` replaces it per server name after `CONNECTION_ID_PATTERN` and uniqueness checks. Each draft's connection passes `validate_connection`; a failure is the draft's `error`.

## Secrets and credentials

A secret never enters a connection; it becomes a credential reference, and the draft carries a value it found apart (`ImportedCredential.value`) for the caller to store. Per environment value or header:
- An exact `${env:NAME}` or `${NAME}` reference keeps naming `NAME` (state `reference`); a `:-default` part is dropped with `default_ignored`. Editor variables are not references: `${userHome}`, `${pathSeparator}` and `${/}` get the vBot host's values, others such as `${workspaceFolder}` stay as unresolved placeholders.
- A VS Code `${input:id}` becomes the missing credential `MCP_<ID>_<TARGET>`, described by the input's prompt.
- Otherwise the value is a secret when its variable or header name contains a secret word (`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `AUTH`, ...) not followed by a describing last word (`PATH`, `URL`, `ID`, `NAME`, ...) and the value is not plain (a switch, a number or an absolute path), or when the value has a known secret shape (`sk-...`, `ghp_...`, `xox...`, `AKIA...`, a JWT, `Bearer <token>`). Every header is a credential, because connections keep no plain header values. A placeholder (`<...>`, `your_...`, `xxx`, `...`, `changeme`) or an embedded reference leaves the credential `missing`; a real value is `provided`.

Warnings are `{code, message}`: `env_file_ignored`, `ignored_fields`, `directory_ignored`, `default_ignored`, `unresolved_placeholder` (a `${...}` left in the command, an argument, a value or the URL; sets `unresolved`), `placeholder_argument` (sets `unresolved`), `secret_in_arguments` and `secret_in_url` (a secret that stays in plain configuration; both set `exposes_secret`), and `remote_bridge` (an `mcp-remote` bridge whose URL vBot can reach directly). Accessors show the English `message`.

## The `import` operation (`MCPService._import`)

Arguments: `source` (string with `contentMediaType: text/plain`, at most 200000 characters), optional `apply`, `servers` (names) and `ids` (name -> connection id).

- Preview (no `apply`) stores nothing and returns `{servers: [...]}`, each `{name, id, connection, error, conflict, selected, enabled, unresolved, disabled, credentials, warnings}`. Credentials are `{name, kind, target, state, description}` with `state` `provided`, `set` (a referenced credential the host already has) or `missing`; values never leave the server. `selected`: no `error`, no `conflict`, no exposed secret. `enabled`: no missing credential, no unresolved placeholder, not turned off by the text.
- Apply parses `source` again and takes the named `servers` (an unknown name fails) or else the `selected` ones; an empty choice fails, and a chosen server with an `error` or `conflict` fails the whole call before anything is stored. Per server it stores `provided` credentials (`host.set_credential`), then saves with `replace=False` and the preview's `enabled`, so a connection waiting for a credential or a placeholder is saved disabled. Servers are saved one after another, not atomically. One INFO line `MCP connections imported (connections=<ids> credentials=<count>)` names no value; the result is `{imported: [<saved records>]}`.

## Accessors

- WebUI: the MCP section's Import action opens `settings/McpImportDialog.svelte`: paste or open a file, preview, review each server (program or URL, credential states, warnings, `error`, `conflict` as "Name in use"), edit a server's connection id (the preview is read again 400 ms after typing stops; a server whose new id resolved its conflict becomes chosen) and optionally type a value per missing credential. `mcpImportPlan` (`lib/mcpSettings.js`) plans the call: `import` with `apply`, then `credential` for each typed value, then `enable` for each connection whose missing credentials were all typed and that is neither unresolved nor turned off.
- WebUI quick fill (`settings/McpQuickFill.svelte`, in the new-connection editor): the text is previewed; exactly one server without `error` and without a `provided` secret fills the form (an id or description the user already typed stays), and its warnings and missing credentials show as notes; any other text opens the Import dialog with it.
- CLI: `vbot extensions run mcp import <file>` or `import -` (standard input) previews; `--apply true`, `--servers '<json-array>'`, `--ids '<json-object>'` apply (document arguments: `cli.md` -> Extension management operations). Agent guidance: `resources/skills/vbot-docs/references/mcp.md`.

## Tests

`tests/resources/extensions/mcp/test_mcp_import.py` (each client format, secrets and credential states, never replacing a connection, unreadable text), `webui/src/lib/__tests__/mcpSettings.test.js` (import plan and sequence), `webui/src/components/__tests__/SettingsMcpPanel.test.js` (dialog with a typed credential, quick fill hand-off), `tests/cli/test_extensions_operations.py` (document argument).
