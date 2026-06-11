# Runtime

Bootstrap entry point. Wires services and manages start/stop lifecycle.

## Interfaces

`core/runtime/interfaces.py` — `typing.Protocol` DI contracts. Only `ConfigProtocol` is constructor-injected; the rest are structural typings used for DI/testing. `core/runtime/__init__.py` re-exports the protocol set plus the `Runtime` class — all protocols except `ProviderCredentialResolverProtocol`.

- `ConfigProtocol` — `get(key: str, default: Any = None) -> Any` (the one injected dependency).
- `LoggerProtocol` — `debug/info/warning/error(msg, *args)`; types the public `logger` attribute.
- Registry/store protocols: `ProviderRegistryProtocol`, `ModelRegistryProtocol`, `ProviderCredentialResolverProtocol`, `StorageManagerProtocol`, `AgentStoreProtocol`, `ToolRegistryProtocol`, `SkillRegistryProtocol`, `ChatSessionManagerProtocol`.
- `RuntimeServices` — the service surface of a *started* runtime as consumed by core modules that genuinely need the whole runtime handle (chat loop, tool dispatch, sub-agent coordination). Read-only properties `agents`, `providers`, `models`, `provider_credentials`, `storage`, `chat_sessions`, `chat_run_manager`, `tools`, `skills`, `extensions` (`ExtensionRegistry | None`), `system_prompts`, `process_manager`, `streaming_chat_loop`, plus `get_adapter(provider_id, connection_id)`. A missing attribute is a wiring bug, not a silently disabled feature — consumers access services directly, never via `getattr` probes. The heavy service types are imported under `TYPE_CHECKING` only, and consumers must likewise import `RuntimeServices` under `TYPE_CHECKING` (a runtime import of `core.runtime` loads `Runtime` and everything behind it — import cycle).

## Runtime class

`core/runtime/runtime.py` — constructor injection via `ConfigProtocol`.

`__init__` resolves the data dir (`DATA_DIR` / `VBOT_DATA_DIR` / `config.data_dir`) and builds `LogManager(level=config.get("LOG_LEVEL", "INFO"), data_dir=...)` before any bootstrap logging. The `vbot.core` logger itself is created later, in `start()`.

### `start()`

Idempotent — a second call logs at debug and preserves the existing service instances. It runs these boot phases in order:

1. Create the `vbot.core` logger, log `Runtime startup initiated`, and record `started_at` (UTC).
2. Build `StorageManager`, `ensure_directories()`, load `settings.json`, and read the `attachment_max_size_bytes` / `speech_upload_max_size_bytes` size limits (positive-int validated, else default with a warning).
3. Instantiate the runtime-owned `AttachmentStore` (`<data_dir>/attachments/`, sized by `attachment_max_size_bytes`); read `<data_dir>/.env` as a fallback credential snapshot **without** mutating `os.environ`; copy prompt fragments.
4. Load provider + model registries; instantiate the OAuth `TokenStore` and the central `ProviderCredentialResolver` (process environment takes precedence over the data-dir fallback).
5. Create `TaskModelService`, then `SpeechService` (STT/TTS + artifacts) and `ImageService` (image generation).
6. Wire `AgentStore` with `defaults_provider=lambda: storage.load_defaults().get("agent", {})` so resolved Agent reads always use the latest persisted defaults without rewriting agent files.
7. Create `ProcessManager`; start its sweeper only when startup happens inside a running asyncio loop.
8. Create `ToolRegistry` + `MemoryService`, then register built-in tools: `read`, `edit`, `glob`, `grep`, `write`, `memory`, `web_fetch`, `web_search` (with a settings resolver so it reads the current `settings.web_search` provider at call time), Home Assistant tools (gated on `HASS_TOKEN`), `process`, `text_to_speech`, `image_generation`.
9. Load the `SkillRegistry` (`<data_dir>/skills`, bundled `resources/skills`, configured `skill_directories`) and register the internal `skill` tool; load the `ExtensionRegistry` (`<data_dir>/extensions` + configured `extension_directories`).
10. Create `ChatSessionManager`, then ensure the bootstrap Agent exists (this happens before channels start — see Gotchas).
11. Create `RecallBackendRegistry`, select the backend from `settings.recall.backend` (fallback `jsonl_scan` on unknown name), and register `session_search` against it.
12. Create `ChatRunManager` (also exposed as `runtime.chat_runs`), the `CommandDispatcher` for built-in slash commands, and resolver-wired non-streaming + streaming `ChatLoop` instances — both constructed with one shared `CompactionService(SummarizationStrategy())` (constructor injection; no post-hoc wiring).
13. Wire `TriggerService` (streaming loop for programmatic Runs, non-streaming loop for command helpers); wire + start `ChannelService` when an event loop is active and register `channel_send` while ≥1 channel is active; wire + start `CronService` when an event loop is active and register the `cron` tool; register `bash` (needs the process manager + trigger service).
14. Create the `SubAgentCoordinator` (owns the in-memory batch tracking) and register sub-agent tools; register the `status` tool; build `SystemPromptManager`.
15. Write one info-level inventory summary (tool + skill counts, usable/total provider + connection counts), set started, and log `Runtime started`.

### Shutdown

- `stop()` — logs `Runtime stopped` if a logger exists, stops `ChannelService` / `CronService` / `ProcessManager` synchronously, clears all service references (including `token_store`), and closes the log manager. Safe to call before `start()`.
- `aclose()` — async variant of `stop()`: same sequence but `await`s the channel / cron / process-manager async cleanup. Accessors running inside an event loop should prefer `aclose()` over `stop()`.

### Service properties

All service properties raise `RuntimeError` before `start()` and after `stop()`, **except `extensions`**, which returns `None` instead of raising. `chat_runs` is a plain attribute (not a property) that is `None` until `start()`.

- `config` — the injected `ConfigProtocol`. Unlike the service properties it is available **before** `start()`; the server uses it for pre-start bind resolution.
- `logger` — public attribute, `LoggerProtocol | None`. Set by `start()`.
- `providers` / `models` — provider and model registries.
- `provider_credentials` — central provider credential resolver; also exposed through `has_provider_credentials(provider_id)` and `get_provider_credentials(provider_id)`. `resolve_environment_credential(key)` resolves a single environment credential with the same precedence (process env first, then data-dir `.env` fallback) — this is what `web_search` and the Home Assistant tools use.
- `token_store` — OAuth token persistence service rooted at `<data_dir>/oauth/`.
- `storage` — `StorageManager` for data-dir/settings/prompt fragments.
- `attachment_store` — runtime-owned `AttachmentStore` rooted at `<data_dir>/attachments/`.
- `speech_upload_max_size_bytes` — max accepted uploaded-audio size for transcription (`settings.json` `speech_upload_max_size_bytes`).
- `agents` — `AgentStore` for agent CRUD/workspaces, wired to `settings.json` `defaults.agent`.
- `tools` — runtime `ToolRegistry` with built-ins registered at startup: `bash`, `cron`, `edit`, `glob`, `grep`, `image_generation`, `memory`, `process`, `read`, `session_search`, `status`, `subagent`, `subagent_result`, `text_to_speech`, `web_fetch`, `web_search`, `write`, plus the internal `skill` tool. `channel_send` is registered dynamically while ≥1 channel is active; the Home Assistant tools (`ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`) only when `HASS_TOKEN` is configured.
- `process_manager` — shared in-memory `ProcessManager` used by `bash` and `process` tools. Owns process sessions, output buffers, TTL sweeping, and Run-scoped cancellation.
- `skills` — `SkillRegistry` loaded from `<data_dir>/skills`, bundled `resources/skills`, and configured extra `skill_directories`.
- `extensions` — `ExtensionRegistry | None` loaded from `<data_dir>/extensions` and configured `extension_directories`. Unlike the other service accessors it does **not** raise before `start()` — it returns `None`.
- `model_tasks` — `TaskModelService` for specialized task-model settings, credential-gated target discovery, and backend-owned option schemas.
- `speech` — `SpeechService` for configured speech-to-text, text-to-speech, and speech artifact lookup.
- `image` — `ImageService` for configured image generation and image artifact lookup.
- `chat_sessions` — `ChatSessionManager` rooted at runtime data dir.
- `recall_backend` — selected `RecallBackend` used by `session_search`.
- `chat_run_manager` / `chat_runs` — shared `ChatRunManager` used by server chat flows and automation triggers.
- `command_dispatcher` — shared `CommandDispatcher` for built-in slash commands, used by server RPC handlers and channel adapters before starting Runs.
- `chat_loop` / `streaming_chat_loop` — resolver-wired non-streaming / streaming `ChatLoop` for server non-SSE and SSE flows respectively.
- `trigger_service` — `TriggerService` for programmatic Run starts and in-memory busy-Session queueing.
- `channel_service` — `ChannelService` for persisted channel configs, adapter lifecycle, and outbound platform delivery rooted at `<data_dir>/channels/`.
- `cron_service` — `CronService` for persisted cron and one-shot scheduled triggers rooted at `<data_dir>/cron/jobs.json`.
- `system_prompts` — `core.prompts.SystemPromptManager` using runtime storage/tools/skills.

### Hot-reload methods

- `reload_skills()` — reload the skill registry from current settings, re-register the internal `skill` tool, and update `SystemPromptManager` so prompt catalogs and provider tool visibility use the new registry without restarting the app.
- `reload_recall_backend()` — reload the configured Recall backend from `settings.recall.backend` and re-register `session_search` so Settings UI backend changes take effect without restart.
- `reload_channel_tool()` — unregister/re-register `channel_send` to match `channel_service.has_active_channels()` so runtime channel enable/disable stays in sync.
- `reload_provider_credentials()` — reload the data-dir `.env` fallback snapshot and rebuild the `ProviderCredentialResolver` from it.

### Adapter factory

`get_adapter(provider_id, connection_id)` builds an async provider-token getter for the selected connection and instantiates the class selected by `ProviderConfig.adapter`. Adapter keys: `openai_compatible`, `openai`, `opencode_go`, `openrouter`, `mistral`, `minimax`, `github_copilot`, `anthropic`. The `openai` adapter is the single `OpenAIAdapter` class that covers both OpenAI Platform API-key access and ChatGPT subscription access; it branches on a per-connection `mode` (set from `ConnectionConfig.mode`) to pick `/chat/completions` vs `/codex/responses`. Every adapter also receives the connection's `mode` as a keyword-only `connection_mode` constructor argument; adapters with a single wire variant accept and ignore it. `api_key` connections resolve the static credential through `provider_credentials` and receive `StaticTokenGetter`; OAuth connections with `OAuthConfig` receive `OAuthTokenGetter` using `runtime.token_store`. OAuth stubs that still have a credential key but no OAuth metadata stay static-credential connections until configured otherwise. Every adapter also gets a provider-scoped, read-only `model_lookup(model_id) -> Model | None` backed by `ModelRegistry`, and — when Debug Mode is enabled — a `ProviderDebugRecorder` built from debug settings, so wire capture is wired into the adapter's HTTP transport. `get_model(provider_id, model_id)` is a thin convenience over `ModelRegistry.get`.

## Constraints & Gotchas

- On first start with an empty data directory, Runtime creates the bootstrap Agent `main` / `Main`. Agent creation also creates the first empty Session and persists it as `current_session_id`.
- Existing data directories with at least one Agent are preserved; Runtime does not add another `main` Agent just because `main` is absent.
- The bootstrap Agent is ensured **before** `ChannelService` starts, so a configured channel targeting `main` can come up during first-start recovery.
- `ChannelService.start()` and `CronService.start()` share the event-loop guard: when runtime startup happens without an active asyncio loop, the service is wired but its listeners are not started.
- Channel startup failures are isolated to the channel service and recorded as failed channel health state; they must not fail `Runtime.start()`.
- Runtime owns the canonical resolver-wired chat loops (including their compaction service) and the canonical `CommandDispatcher`. Server/accessor code must reuse `runtime.chat_loop` / `runtime.streaming_chat_loop` / `runtime.command_dispatcher` — the server no longer probes for them or constructs fallbacks, so a stub runtime handed to `create_app` must provide them.
