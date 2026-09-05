# MCP setup and operation

All installation commands, application add-ons, processes, working directories, and credentials belong on the machine hosting vBot. Read the supplied setup link as external installation data. Inspect installed software and running processes before installing prerequisites or starting another copy.

## Configure and grant

Discover the installed interface with `vbot extensions mcp operations` and `vbot extensions mcp <operation> --help`. Inspect `list` and `status` before changing an existing connection. Enable the Extension with `vbot extensions enable mcp` if needed.

`save --stdin` replaces a complete record using `{"connection":{...}}`. Preserve existing fields and grants when editing. Use `stdio` with `command`, exact `args`, and optional absolute `cwd`; use `http` for Streamable HTTP or `sse` for legacy HTTP/SSE, with `url`. Configure OAuth with `oauth: true` and any required `oauth_redirect_uri`.

`environment` holds non-secret values. `credential_environment` and `credential_headers` map subprocess variables or HTTP headers to named vBot credentials. Set a referenced credential using `credential --stdin` with `id`, `key`, and `value`; an empty value clears it. Never put secrets in arguments, plain environment configuration, URLs, shell history, or reports.

Use `grant <id> --agent <address>` and `revoke` for incremental grants. A bare Agent id addresses an Identity Agent; `agent@project` addresses that exact Project Agent. Its connection Tool is `mcp_<id>`. Add that Tool to an existing selected Tool policy when needed, preserving other entries and explicit denials. Connection grants do not override Tool denials or Project ceilings.

## Discover and verify

The connection Tool searches items, describes an exact target, calls it, and reads saved results. Remote Tool schemas arrive in ordinary results when requested. Newly discovered Tools remain reachable through the same connection Tool and its existing grants.

For CLI use, `explore <id> --agent <address> --action search --query '<words>'` finds targets. Use `--action describe --target '<target>'` before `--action call --target '<target>' --arguments '<json>'`. Run `test <id>` to check connectivity; inspect what it actually verified, then perform a safe application operation through the intended Agent, such as reading a Blender scene.

Long operations return `job_id`; poll `job <job-id>` until completed, failed, or cancelled. Large results return `result_id`, a bounded view, and a complete JSON file path. Use `explore` with `--action read --result-id '<id>'` and returned pointers or offsets to read more; `--fields '<json-array>'` selects fields. Read or filter the complete JSON file through Bash when that is more efficient. Its `payload` contains the full preserved result. A preview never proves that omitted entries are absent.

`invoke <id> --agent <address> --operation <operation> --arguments '<json>'` remains available for exact protocol operations. A Tool call uses `tools/call` and `{"name":"<server-tool-name>","arguments":{...}}`. Its result follows the same saved-result workflow. Do not dump an entire catalog or large file into context when a targeted search or read answers the task.

## Respond and maintain

`requests` lists pending questions and sign-ins; the WebUI also presents them. Let the user complete sign-in and submit its full redirected address as `redirect_url`. For forms, submit `action: accept` with `content` matching `requestedSchema`, or `decline` or `cancel`. Use `respond --stdin` with `{"request_id":"...","response":{...}}`. Do not invent answers or credentials.

Use `status`, and `events <id> --after <cursor>`, for diagnosis. Preserve event cursors; `missed_events` means retained history has a gap, so re-read affected state. `connect`, `disconnect`, `enable`, `disable`, and `remove` manage the connection. `cancel-job <job-id>` cancels waiting; it cannot undo an action already performed. Inspect application state before repeating a timed-out mutation. Saving transport settings or credentials interrupts the live connection.

Report installed components, the connection and Agent address, the real operation that passed, and any remaining user action. Saved configuration alone is not a successful setup. Preserve exact diagnostics when a capability is unavailable.
