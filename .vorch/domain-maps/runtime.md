# Runtime

Bootstrap entry point. Wires services and manages start/stop lifecycle.

Blocking in-process work crosses named `BoundedWorkerPool` boundaries from `core/utils/workers.py`: each pool owns a dedicated executor, admits at most its worker count per Event Loop, and defers cancellation until an already-started mutation settles; continuous Terminal I/O stays on its own executor so it cannot consume parser/prompt/Session/Tool capacity.

## DI Contracts

`core/runtime/interfaces.py` holds `typing.Protocol` contracts. Only `ConfigProtocol` is constructor-injected; the rest are structural typings. `RuntimeServices` is the read-only service surface of a *started* runtime for core modules coordinating across it - consumers access services directly (a missing attribute is a wiring bug, never a `getattr` probe), and Chat deliberately does not consume it: Runtime builds Chat-owned `ChatLoopDependencies`, and Chat projects those into Run-local context. Heavy service types import under `TYPE_CHECKING` only - a runtime import of `core.runtime` loads `Runtime` and everything behind it (import cycle). The central credential contract is `ProviderCredentialResolverProtocol` (`providers.md` -> Usable; details in `providers/connections.md`).

## Bootstrap

`start()` is idempotent and runs dependency-ordered phases. Ordering constraints worth knowing before touching bootstrap:

1. **Storage & settings** - logger + `StorageManager` + settings load. Invalid Settings never abort startup: malformed JSON or root shape falls back to defaults, schema-invalid keys omit individually while siblings stay live, the source file never rewrites, and mutations stay strict so they cannot overwrite invalid data. Data-dir creation failure is fatal.
2. **Attachments & credentials** - AttachmentStore plus the `.env` fallback snapshot read without mutating `os.environ`.
3. **Providers & models** - tolerant load of bundled config + Custom Provider overlay; newer complete Model DB root selected (roots never mix); OAuth token store and the central credential resolver wired.
4. **Task services, agents, processes** - TaskModelService + per-task services; AgentStore with live defaults provider; ProcessManager. Invalid agents degrade individually.
5. **Tools, extensions, skills** - ToolRegistry + built-in registration, then extension/skill registries and `skill`/`skill_manage`. Unreadable roots diagnose-and-omit, never fatal.
6. **Sessions, recall & chat** - ChatSessionManager with the Session-scoped `history` Tool registered immediately (**before** extension tools, preserving built-in name ownership); Project/Agent resolution and bootstrap Agent (**before** channels - see Gotchas); recall registry (`jsonl_scan` construction failure is fatal - no canonical fallback), ChatRunManager, Reflection, titles, ChatLoopDependencies, both chat loops sharing one CompactionService.
7. **Automation surface** - TriggerService (streaming loop for triggers, non-streaming for manual Compaction); TerminalManager; BootstrapService; only then the end-to-end CommandDispatcher receiving applied Extension Commands; ChannelService start + `channel_send`; `cron`/`bash` when dependencies exist. Bootstrap registers no Tool and does not activate here.
8. **Sub-agents & prompts** - SubAgentCoordinator, sub-agent tools and `status`, Extension Tools applied **last**, SystemPromptManager, one info startup-inventory summary.

## Shutdown

- `stop()` stops producers (Channels/Cron/Bootstrap), usage collector, Process/Terminal managers (killing every tracked process and Terminal tree), the temp-file sweeper, clears service references, closes logging. Safe pre-start.
- `aclose()` is the async variant accessors in event loops should prefer: producers stop first, then Trigger completion delivery, Reflection, titles, and ChatRunManager close - rejecting new work, cancelling queued work, waiting active Runs plus cancellation cleanup - before Provider usage, Process/Terminal managers, and temporary files close. No Runtime-owned background task may outlive its services.

## Service properties

All service properties raise `RuntimeError` outside a started runtime, **except `extensions`** (returns `None`); `chat_runs` is a plain attribute until `start()`. `config` is available pre-start (bind resolution). Non-obvious members beyond self-describing services:

- `provider_credentials` - the central resolver also exposed via `has_provider_credentials`/`get_provider_credentials`; `resolve_environment_credential(key)` resolves env-first without exposing values, `environment_credential_source(key)` reports provenance. Web Search, Extensions, and Channels consume this shared instance so live `.env` reloads reach every credential-backed subsystem.
- `tools` - full built-in roster lives in source; `analyze_image` registers but Chat gates its Model-facing visibility on route capability plus ImageService availability; `channel_send` registers dynamically while >=1 valid Agent owns an enabled Channel (adapter liveness irrelevant).
- `file_read_state` - the shared read-before-write guard behind read/write/edit and chat `@`-mention snapshots.
- `command_dispatcher` - the canonical stable dispatcher; server/accessor code must reuse `runtime.chat_loop` / `runtime.streaming_chat_loop` / `runtime.command_dispatcher` - no probing, no fallback construction (stub runtimes must provide them).

### Hot-reload seams

All reload methods keep registry/service identity stable so already-wired consumers observe changes without restart:

- `reload_custom_providers()` - Settings-owned Providers + manual Models into existing registries.
- `maybe_refresh_local_catalogs(force=False)` - staged copy refresh for auto-refresh Connections, published atomically after validation; failures never raise and leave the last database untouched.
- `reload_skills()` - reload registry, drop project/agent caches, re-register skill Tools, update prompts. The runtime also owns `agent_skills_dir`, `skills_for(project_id, identity_agent_id=None)` (agent layer applies only to an **existing identity** agent - double-checked via `agents.exists`, so a stray unowned skills dir never layers), and `invalidate_agent_skills`.
- `reload_recall_backend()` re-registers both Recall Tools from settings without restart.
- `reload_keep_awake()` holds/releases the Windows power request per persisted setting (no-op elsewhere).
- `reload_channel_tool()` syncs `channel_send` registration with enabled-Channel existence.
- `reload_environment_credentials()` refreshes the `.env` fallback inside the same resolver instance so every startup-injected consumer sees key/secret/token writes immediately.
- `reload_extensions()` (async) rebuilds the whole extension layer restart-equivalently under `_extension_reload_lock`: fresh settings, detach old Tools/Commands, shutdown hooks, module purge, fresh registry, swap, re-apply to stable registries + Recall + prompt blocks, then startup hooks awaited on the serving loop. Enabling routes here; disabling takes `apply_extension_disabled_change(newly_disabled)` instead: surgical deactivation per name plus prompt-block refresh, with a Recall guard rebuilding toward the Built-in default when a deactivated extension owned the active backend (persisted setting untouched). Both hold `_extension_reload_lock`, serializing rapid toggles last-write-wins; the brief window where a Run can still dispatch into a shut-down old extension is accepted (per-handler fail-open isolation) - no drain.

### Adapter factory

`get_adapter(ConnectionRef)` instantiates the class selected by `ProviderConfig.adapter` (authoritative map in `runtime.py`). `ConnectionRef` is a frozen dataclass in `core/providers/accounts.py` bundling the provider id and its compositional `provider:connection[:account]` connection id; the same reference shape serves `get_connection_token_getter()` / `get_connection_token_extra()`. The single `OpenAIAdapter` class covers Platform API-key and subscription access, branching on the connection's `mode`; every adapter receives `connection_mode` keyword-only (single-wire adapters ignore it). API-key connections get `StaticTokenGetter` via the credential resolver, OAuth connections get `OAuthTokenGetter` over `token_store`; stale OAuth stubs holding only a key stay static until configured. Every adapter receives a provider-scoped read-only `model_lookup` and, in Debug Mode, a `ProviderDebugRecorder` fed through the shared transport (the OpenAI subscription WebSocket feeds it at exchange boundary).

## Constraints & Gotchas

- First-start recovery creates a bootstrap Agent (`main`/`Main`, shifting to free `main-N` when an invalid preserved directory occupies the name), including first empty Session as current pointer. Existing directories with any valid Agent are preserved - no second `main`.
- Invalid individual Agent/Project configs skip individually; Projects need only `project_id` + `cwd`.
- The bootstrap Agent ensures **before** ChannelService starts so a channel targeting `main` recovers on first start.
- Channel/Cron starts and the usage collector share the event-loop guard: wired but not started when no loop exists. Channel startup failures isolate to failed health state; Cron storage corruption disables scheduling without failing startup and protects state from overwrite.
