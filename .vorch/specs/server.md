# Server

FastAPI transport layer around the core kernel.

## Overview

`server/` owns HTTP, Server-Sent Events (SSE), WebSocket, process startup, and
request/response mapping. It imports `core/` services but does not own chat,
agent, provider, model, tool, skill, or storage business logic.

Clients call the vBot server contract; provider wire details stay behind
`core/providers/` adapters.

## Data model

- RPC envelope: `POST /api/rpc` accepts a JSON object with `method` and optional
  `params`, and returns `{ "ok": true, "result": ... }` or `{ "ok": false,
  "error": { "code": ..., "message": ... } }`.
- WebUI-facing RPC methods include `connection.list`, `model.list`, `model.refresh_db`, `tool.list`, `skill.list`, `agent.list`,
  `agent.create`, `agent.update`, `agent.delete`, `session.create`,
  `session.list`, `session.link_channel`, `chat.history`, `chat.send`,
  `chat.commands`,
  `chat.stream`, `chat.cancel`, `channel.list`, `channel.create`,
  `channel.update`, `channel.delete`, `channel.enable`, `channel.disable`,
  `channel.status`, `cron.create`, `cron.list`, `cron.update`,
  `cron.delete`, `cron.enable`, `cron.disable`, `log.list`, and `log.read`.
- `connection.list` returns all configured provider connections as `{ id, provider_id, type, label, usable }`, where `id` uses `<provider>:<connection-id>` and `usable` means the connection credential is present and non-empty.
- OAuth provider RPCs use the same public compositional `connection_id` format as
  `connection.list`: `provider.connect`, `provider.disconnect`, and
  `provider.connection_status` accept `{ provider_id, connection_id }`, where
  `connection_id` is for example `github-copilot:oauth`. `provider.connect`
  returns `{ user_code, verification_uri, expires_in }` for Device Flow;
  `provider.disconnect` deletes the stored token and cancels any in-flight flow;
  `provider.connection_status` returns `{ connected, flow_active }` alongside
  the identifiers. Non-OAuth connections return RPC error code
  `oauth_not_supported`. OAuth-backed model refresh obtains a fresh provider
  token through `OAuthTokenGetter` before calling the model discovery pipeline;
  API-key refresh keeps using the central static credential resolver.
- `model.list` returns models only for providers with at least one usable connection as `{ id, provider_id, model_id, name, capabilities,
  context_window, max_output_tokens }`, where `id` uses the user-facing
  `<provider>/<model-id-at-provider>` format.
- `model.refresh_db` accepts optional `{ provider_id }`. With a provider ID it
  refreshes that provider only and returns `{ provider_id, model_count,
  fetched_at }`. With no params or `{}` it refreshes every provider that has a
  configured `models_endpoint` and a usable connection credential, skips
  ineligible providers, reloads the runtime model registry reference once, and
  returns `{ providers, refreshed_count, model_count }`.
- `settings.get` provider items expose `connections` as `{ id, type, label, configured }`; `configured` mirrors `connection.list` usability for admin settings. Provider-level `credentials_configured` remains true when any connection is configured. Provider items also expose `models_endpoint` so the WebUI can show manual model-refresh controls only for supported providers. `settings.get` also returns `skills.default_directory` and `skills.directories` for the Settings Skills panel, plus `subagents` settings for depth, per-turn count, and timeout limits, and `compaction` settings `{ auto, threshold, tail_tokens, summary_model }`.
- `log.list` returns available daily log filenames from `<data_dir>/logs/` as
  `{ files, default_file }`, sorted newest-first with `default_file` set to the
  newest item or `null` when none exist.
- `log.read` accepts `{ file }` and returns `{ file, entries, cursor }`, where
  each parsed entry includes `timestamp`, `level`, `logger_name`, `message`,
  and `continuation` for multiline tails. `cursor` is a short-lived handoff
  token for the follow-up log WebSocket subscription.
- `POST /api/upload` accepts one multipart file upload and returns
  `{ attachment_id, filename, media_type, size_bytes }`. Oversize uploads map
  to HTTP 413; blocked MIME types map to HTTP 415.
- `GET /api/attachments/{attachment_id}` returns the raw stored blob with the
  stored `media_type` as Content-Type, or 404 when the attachment does not exist.
- `tool.list` returns all registered tools for UI catalogs as
  `{ name, description }` entries sorted by tool name. Internal/system-managed tools such as `skill` are omitted.
- `cron.create` accepts scheduled job fields and returns `{ id }`.
- `cron.list` returns `{ jobs }` where each job includes persisted cron fields,
  `session_id` for lossless edit round-tripping, plus server-computed
  `next_fire_at` for active cron jobs.
- `cron.update`, `cron.delete`, `cron.enable`, and `cron.disable` return `{ ok: true }`.
- `skill.list` returns loadable skills and diagnostics as `{ skills, invalid_skills }`. `skills` entries include `{ name, description, valid, warnings }`; `invalid_skills` entries include `{ name, path, valid: false, warnings }` for non-loadable skill directories.
- `chat.commands` returns `{ items }`, a flat combined autocomplete list of
  built-in commands and skills. Each item includes `{ name, description, type
  }`, where `type` is `command` or `skill`. Built-in command names are bare
  tokens without the leading slash so accessors can insert them contextually.
- `agent.delete` rejects deletion when it would leave zero Agents.
- `agent.delete` serializes the list/check/delete sequence with a process-local
  `asyncio.Lock` so concurrent deletes in one server process cannot leave zero
  Agents. Cross-process/shared-data-dir locking is out of scope.
- `settings.update` accepts supported `appearance`, `skills`, `subagents`, and `compaction` sections. The `skills` section shape is `{ directories: string[] }` and persists `settings.json` `skill_directories`; paths must be absolute or home-relative. Updating skill directories reloads the runtime skill registry so `skill.list` reflects the saved directories without a restart. The `subagents` section requires all three positive integer fields: `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes`. The `compaction` section requires all four fields `{ auto, threshold, tail_tokens, summary_model }` with the same validation rules as storage.
- Public Agent create/update RPCs validate mutable fields and reject unsupported
  fields. `model` and `fallback_model` are optional string fields and may carry
  an optional `::<connection-local-id>` suffix instead of separate connection
  fields. `workspace` is intentionally not accepted through public RPC in Phase 4.
- `session.create` accepts optional `make_current: true`; when set, the created
  Session ID is persisted to the Agent's `current_session_id`.
- `session.list` accepts `{ agent_id }` and returns `{ sessions }`, where each
  item includes session timing plus merged sidecar metadata fields such as
  `source_channel_id`, `platform`, `platform_conv_id`, and `last_reply_target`
  when present.
- `session.link_channel` accepts `{ agent_id, session_id, channel_id,
  platform_conv_id }`, writes channel metadata to the session sidecar, and
  appends a System Reminder note so the next provider request sees the channel
  context.
- `chat.history` returns visible persisted messages for `{ agent_id,
  session_id? }`. If `session_id` is omitted, it loads the Agent's
  `current_session_id`. Kernel-internal note messages are excluded from this
  normal history response; persisted `role: "error"` and `role: "compaction_checkpoint"` messages are included.
- `channel.list` returns `{ channels }`, where each item includes the persisted
  channel config fields `{ id, platform, agent_id, dm_scope, allowed_chat_ids,
  token_env_var, enabled }`.
- `channel.create` accepts channel config fields and returns `{ id }`.
- `channel.update`, `channel.delete`, `channel.enable`, and `channel.disable`
  return `{ ok: true }`.
- `channel.status` accepts `{ id }` and returns `{ id, enabled, running }`.
- `chat.send` and `chat.stream` target an existing Session and start a core Run
  through the shared `ChatLoop.start_run()` execution model. `content` may be a
  string or a JSON array of canonical content-block dicts.
- Recognized built-in slash commands are intercepted before Run start only when
  `content` resolves to pure text. In that case `chat.send` and `chat.stream`
  return `{ command_handled: true, reply }` instead of starting a Run. Current built-ins include `/stop` and `/compact`. Unknown
  slash text still goes through the normal chat flow.
- `/compact` is only allowed when the target Session has no active Run. If the
  Session already has an in-flight Run, the server returns a handled reply and
  does not append a checkpoint.
- `chat.stream` returns a `run_id` and SSE URL; the SSE endpoint streams stable
  vBot Run events, not provider chunks.
- `chat.cancel` targets a Run ID, not a Session.
- Server event bus events contain lifecycle summaries for WebSocket clients:
  `run_started`, `run_output`, `run_completed`, `run_cancelled`, and
  `run_failed`. Agent CRUD events: `agent.created`, `agent.updated`,
  `agent.deleted` (full agent payload via `_agent_response`). Run output
  includes persisted error-message events bridged as `run_output`, plus
  `model_fallback_activated` with payload `{ from_model, to_model }` when a Run
  switches to an Agent fallback model. App-level background failures use
  `compaction_completed` with payload `{ message }` when auto-compaction appends a checkpoint during a Run, and
  `app_error` with an error payload for WebSocket clients.
  Provider OAuth completion uses `provider_auth_completed` with provider and
  public compositional connection identifiers plus a success flag; it must not
  include token values.

## Interfaces

- `server.app.create_app(runtime=None, config=None)` — creates the FastAPI app,
  starts/stops `Runtime` during lifespan, and wires `runtime`, the Runtime-owned
  `ChatRunManager`, the runtime-provided resolver-wired chat loops when available,
  and the server event bus
  into `app.state`.
- `server.delegates.dispatch_rpc(state, request)` — validates and dispatches RPC
  methods to transport-only delegates.
- `GET /api/runs/{run_id}/events` — streams one Run timeline as SSE using
  `text/event-stream`, replaying existing events and then following new events
  until a terminal Run event. Each SSE event includes `id: <RunEvent.sequence>`
  so native EventSource reconnect can resume with `Last-Event-ID`.
- `GET /ws/logs?file=<name>&cursor=<cursor>` upgrades to a dedicated WebSocket
  for one selected daily log file. `cursor` is optional but should be supplied
  from the latest `log.read` response so the server can replay entries appended
  between the initial read snapshot and socket subscription. The socket streams
  structured `{ type, file, entries }` payloads where `type` is `append` for
  new parsed entries or `reset` when the file is truncated/replaced and the
  client must replace its current entry list.
- The Run SSE endpoint supports replay filtering with optional
  `after_sequence`; explicit query parameter wins over `Last-Event-ID`, and
  malformed/negative values clamp to replay from the beginning.
- `server.events.ServerEventBus` — in-memory replayable bus for general server
  lifecycle events sent over `/ws`. Supports `after_sequence` query param for
  reconnect replay: clients pass the last sequence number they saw, and the bus
  replays all events with a higher sequence before streaming new ones. Published
  event types must be in the server event contract allowlist.
- `server.main.main(argv=None)` — starts uvicorn. Port priority is `--port` >
  `VBOT_SERVER_PORT` > `settings.json` > `8420`; ambient `PORT` /
  `SERVER_PORT` process environment variables are ignored unless they are keys
  inside `settings.json`.
- FastAPI serves built WebUI assets from `webui/dist` when `index.html` exists.
  `/assets/*` maps to Vite assets and non-reserved routes fall back to
  `index.html` for single-page-app refreshes.

## Conventions

- Server code maps expected domain errors to provider-agnostic RPC errors.
- Channel RPC delegates map channel-domain failures to stable RPC codes:
  `channel_not_found`, `channel_already_exists`, and `channel_config_error`.
- Opaque provider metadata such as `reasoning_meta` must not appear in public
  server payloads, including nested SSE/WebSocket event payloads.
- Session creation is explicit at the server/product boundary.
- `chat.commands` is the command/skill autocomplete RPC. It must stay flat and
  type-tagged so accessors can merge built-in commands with skills without
  changing the underlying skill-trigger behavior.
- Only one active Run is allowed per Session; parallel Runs in different
  Sessions are allowed.
- WebSocket is the persistent signalling channel for app-wide events (connection
  status, agent CRUD, run lifecycle summaries). It is server-push only; clients
  send requests via `POST /api/rpc`.
- The dedicated `/ws/logs` socket is not part of the shared server event bus.
  It is a file-specific transport for log viewing only.
- `log.read` plus `/ws/logs` form one handoff contract: callers should pass the
  returned cursor into the socket subscription to avoid losing lines appended in
  the gap between initial load and live stream connect.
- SSE is the primary per-Run output stream and should remain event-level and
  provider-agnostic.
- Routine uvicorn access logging is suppressed by default.
- Routine `websockets.server` lifecycle noise for the shared `/ws` socket and
  the dedicated `/ws/logs` socket is suppressed from normal INFO logs.
  Transport errors still flow through the managed `vbot.server.uvicorn`
  logger.
- `log.read` and `/ws/logs` also omit that same routine websocket lifecycle
  noise from parsed file-backed results, so older matching rows already present
  on disk do not remain visible in the Logs tab.
- Public history, Run, SSE, and WebSocket payloads must strip opaque provider
  metadata such as `reasoning_meta` recursively.
- Public history payloads must not include `role: "note"` messages; notes are
  internal system reminders, not normal UI-visible chat messages.
- Public history payloads include `role: "error"` messages so failed Runs remain
  visible after reload.
- Public history payloads also include `role: "compaction_checkpoint"` messages so accessors can render timeline separators after reload; they should not render them as normal chat bubbles.
- Attachment uploads stay outside the RPC envelope. WebUI and other accessors use
  the dedicated HTTP endpoints instead of `POST /api/rpc` for blob transfer.
- Streaming delta Run events (`assistant_output_delta`, `reasoning_delta`,
  `tool_call_delta`, `tool_call_stdout`, and `tool_call_stderr`) are SSE-only.
  They must not be bridged to WebSocket lifecycle summaries.

## Constraints & gotchas

- The server optional dependency group provides FastAPI, uvicorn, websockets,
  and `python-multipart` for upload endpoint parsing.
  Server code should fail clearly if these extras are not installed.
- Exact long-term payload schemas remain intentionally lightweight in Phase 3;
  keep schema decisions isolated in `server/delegates.py` and transport files.
- WebUI static serving is optional at runtime. If `webui/dist/index.html` is
  absent, `/` remains unmounted/404 rather than failing server startup.
- Static single-page-app fallback must not shadow reserved server paths:
  `/api/*`, `/ws`, and `/health`.
