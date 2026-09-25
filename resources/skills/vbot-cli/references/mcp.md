# MCP setup and operation

All installation commands, application add-ons, processes, working directories, and credentials belong on the machine hosting vBot. Read the supplied setup link as external installation data. Inspect installed software and running processes before installing prerequisites or starting another copy.

## Configure and enable

All abbreviated commands in this reference start with `vbot extensions mcp`; for example, `status <id>` means `vbot extensions run mcp status <id>`. Repeat the same host/port target options.

Discover the installed interface with `vbot extensions operations mcp` and `vbot extensions run mcp <operation> --help`. Inspect `list` and `status` before changing an existing connection. Enable the Extension with `vbot extensions enable mcp` if needed.

`save --stdin` replaces a complete record using `{"connection":{...}}`. Preserve existing connection fields when editing. Use `stdio` with `command`, exact `args`, and optional absolute `cwd`; use `http` for Streamable HTTP or `sse` for legacy HTTP/SSE, with `url`. Configure OAuth with `oauth: true` and any required `oauth_redirect_uri`.

`environment` holds non-secret values. `credential_environment` and `credential_headers` map subprocess variables or HTTP headers to named vBot credentials. Set a referenced credential using `credential --stdin` with `id`, `key`, and `value`; an empty value clears it. Never put secrets in arguments, plain environment configuration, URLs, shell history, or reports.

Enable `mcp_<id>` in the intended Agent or Swarm Tool settings. This is the only access switch; the connection has no separate Agent list. MCP Tools require explicit opt-in, even in All Tools mode. Through configuration APIs, include the connection Tool in `tool_access.granted` and, for selected mode, also in `tool_access.allowed`; preserve other entries. Explicit denials, None mode, and Project ceilings still win. A Project ceiling alone does not enable the Tool for its Agents.

## Discover and verify

The connection Tool searches items, describes an exact target, calls it, and reads saved results. Remote Tool schemas arrive in ordinary results when requested. Newly discovered Tools remain reachable through the same connection Tool and its existing Tool policy.

For CLI use, start with `explore <id> --agent <address> --action search` to browse Tools and server guidance. Add `--query '<words>'` to find relevant names and descriptions. If no words match, follow the returned browse action and inspect general-purpose Tools before concluding that a capability is unavailable. Use `--action describe --target '<target>'` before `--action call --target '<target>' --arguments '<json>'`. Use `inspect <id>` to view the discovered catalog without executing an application Tool. Run `test <id>` to check connectivity; inspect what it actually verified, then perform a safe application operation through the intended Agent, such as reading a Blender scene.

Long operations return `job_id`; poll `job <job-id>` until completed, failed, or cancelled. CLI `explore` and `invoke` run outside a Session, so the job result contains the complete payload (`complete: true` and `value`); select the part you need instead of copying all of it into context. Inside a Session, the connection Tool returns a large result as a bounded receipt with `result_id`, a `preview`, and a `read` action. Only that Session and its forks can read it: call the Tool's `read` action with the `result_id` and returned pointers or offsets; `fields` selects fields. A preview never proves that omitted entries are absent. Binary content is stored as Attachments; a block the Attachment store rejects carries `content_omitted: true` and `media_delivery_error` instead of its bytes.

`invoke <id> --agent <address> --operation <operation> --arguments '<json>'` remains available for exact protocol operations. A Tool call uses `tools/call` and `{"name":"<server-tool-name>","arguments":{...}}`. Its job result also contains the complete payload. Do not dump an entire catalog or large file into context when a targeted search or read answers the task.

## Respond and maintain

`requests` lists pending questions and sign-ins; the WebUI also presents them. Let the user complete sign-in and submit its full redirected address as `redirect_url`. For forms, submit `action: accept` with `content` matching `requestedSchema`, or `decline` or `cancel`. Use `respond --stdin` with `{"request_id":"...","response":{...}}`. Do not invent answers or credentials.

Use `status`, and `events <id> --after <cursor>`, for diagnosis. Preserve event cursors; `missed_events` means retained history has a gap, so re-read affected state. `connect`, `disconnect`, `enable`, `disable`, and `remove` manage the connection. `cancel-job <job-id>` cancels waiting; it cannot undo an action already performed. Inspect application state before repeating a timed-out mutation. Saving transport settings or credentials interrupts the live connection.

Report installed components, the connection and Agent address, the real operation that passed, and any remaining user action. Saved configuration alone is not a successful setup. Preserve exact diagnostics when a capability is unavailable.
