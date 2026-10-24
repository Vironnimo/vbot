# Runtime

Bootstrap entry point. Wires services and manages start/stop lifecycle.

`runtime.py::Runtime` owns lifecycle and the stable public access/reload surface. `_bootstrap.py` assembles its dependency-ordered service graph with resource/automation composition in `_bootstrap_services.py`, `_shutdown.py` runs the dependency-ordered shutdown steps of `stop()`, `aclose()` and failed-startup cleanup, `_service_graph.py` holds the typed service references and readiness checks, and `_settings.py` coordinates live Settings effects plus reload generation/publication. These and the other `core/runtime/_*.py` files are internal files of the same Runtime owner, not additional service layers.

Blocking in-process work crosses named `BoundedWorkerPool`s, and owners that must finish admitted work before honoring cancellation use the shared settling helpers (`settle_before_cancelling`, `finish_despite_cancel`, `cancel_and_wait`) rather than their own `shield` or `wait` loops; all live in `core/utils/workers.py`, documented in `runtime/workers.md`.

## DI Contracts

`core/runtime/interfaces.py` holds `typing.Protocol` contracts. Only `ConfigProtocol` is constructor-injected; the rest are structural typings. `RuntimeServices` is the read-only service surface of a *started* runtime for core modules coordinating across it - consumers access services directly (a missing attribute is a wiring bug, never a `getattr` probe), and Chat deliberately does not consume it: Runtime builds Chat-owned `ChatLoopDependencies`, and Chat projects those into Run-local context. Heavy service types import under `TYPE_CHECKING` only - a runtime import of `core.runtime` loads `Runtime` and everything behind it (import cycle). The central credential contract is `ProviderCredentialResolverProtocol` (`providers.md` -> Usable; details in `providers/connections.md`).

Construction order other domains rely on: the canonical `UsageRecorder` follows the Model registry and canonical Sessions (its generation/lineage source), imports retained historical Usage before any Agent lifecycle operation, and is injected into Task services, Chat, Compaction, titles and Statistics (`usage.md`); async shutdown drains those producers and the Statistics readers before closing it, and failed-startup cleanup closes it too; `canonical_databases()` and offline declarations include `model-usage.db`. AttachmentStore collects unreferenced Session-owned attachments and expired unused uploads right after Sessions open, before any producer is admitted, and again only after a clean async shutdown drain (`attachments.md`). `ScratchAreas` follows Sessions and the Run manager, which supply its generation lookup and busy snapshot (`tools/scratch.md`).

## Bootstrap

`start()` is idempotent and runs dependency-ordered phases (`_bootstrap.py`; every phase and its services: `runtime/lifecycle-steps.md`). Any exception after startup work begins runs the synchronous shutdown steps (Shutdown) for every service built so far (`clean_up_failed_startup`), which clears references and closes logging; a failing step is logged with its step name but never raised, the failure is logged once at ERROR (`Runtime startup failed`), and the startup error is re-raised unchanged. The same Runtime can retry after failure (`tests/core/runtime/test_runtime_lifecycle.py`). Ordering invariants worth knowing before touching bootstrap:

- Invalid Settings, Agents, Projects and unreadable Tool/Extension/Skill roots degrade individually and never abort startup; data-dir creation failure and a failing `sqlite_fts` construction (the Recall fallback below which no registration remains) are fatal.
- Canonical Sessions open before `UsageRecorder` and every request producer (DI Contracts).
- Interrupted work settles before its owners start: a pending Agent rename (Constraints) and then interrupted archive operations, which the archive finishes or undoes in every startup mode (`archive.md` -> Recovery), complete before the Channel and Cron starts.
- Cron/Channels start after CommandDispatcher and recovery; accepted Accessor inputs install before their tasks run. Bootstrap activates at server readiness (`activate_bootstrap`).
- Extension Tools are applied **last**, after built-in and Sub-Agent Tools; SystemPromptManager follows them.
- The token estimation encoding starts loading only after startup: started earlier, it slowed startup by contending for the GIL.

Once startup is complete the performance monitor starts in every mode (a passive observer); normal startup then starts, in this order, the Provider usage sampler, Recall indexing, the archive retention sweep (`archive.md` -> Retention), the Scratch sweep, the configuration backup schedule (`_config_backups.py`: a backup at start, a check every 5 minutes that backs up changed configuration files, one more at shutdown; `database.md` -> Configuration backup) and the Librarian's schedule (`automation/librarian.md`).

`safe_startup_mode` is only `verification` or `test`. It constructs the core graph but suppresses Extension dependency activation and Extension loading, Bootstrap activation, the Channel and Cron starts, shell-command recovery, the release of `RETIRED_CANONICAL_DATABASES` (`database.md`) and every post-startup service above except the performance monitor (`tests/core/runtime/test_runtime_lifecycle.py`). Normal startup alone activates managed Extension dependencies before Extension discovery.

`Runtime(..., operational_producers="disabled")` keeps normal Extension dependency activation, loading and catalog assembly while suppressing automatic operational producers: temporary-file/Terminal/command sweepers and command recovery, Channel/Cron starts, Bootstrap activation, usage sampling, Recall indexing, Archive retention, Scratch sweeping, configuration backups and the Librarian. It leaves the passive performance monitor and lifecycle/recovery wiring intact. Disposable Eval workers use this policy so their Tool and prompt catalogs match production; source-only Provider/credential probes use `safe_startup_mode="verification"` instead. Neither policy prevents work a caller explicitly requests through a service.

## Shutdown

`stop()`, `aclose()` and failed-startup cleanup run one dependency-ordered step list (`_shutdown.py`; every step and why it sits there: `runtime/lifecycle-steps.md`); failed-startup cleanup runs the synchronous operations like `stop()` but only logs step failures.

- Order contract: `aclose()` drains admitted Extension work and stops the Provider usage sampler before it withdraws readiness; Session activity closes right after, so shutdown publishes no finished work; producers (Channels, Cron, Bootstrap, archive retention, configuration backups, shell commands, Trigger delivery, goals, Reflection, the Librarian, titles) stop before ChatRunManager drains the Runs; what Runs and Provider calls used (Sub-Agent activity files, video jobs, Speech, Provider usage, Debug traces, wire observations) settles after them; processes, Terminals and temporary state follow; the databases close last, canonical Sessions after everything else. An active performance Recording is discarded, not written. No Runtime-owned background task may outlive its services.
- `stop()` runs the same steps synchronously and is safe pre-start. It withdraws readiness before its first step, fires Extension shutdown handlers without draining admitted Extension work, and skips the steps that need the Event Loop (Session activity, shell commands, Trigger delivery, goals, Reflection, titles, Runs, Sub-Agent activity files, followed video jobs, Debug traces, attachment collection).
- Failure isolation: a step that raises an `Exception` is logged at ERROR with its step name and traceback while logging is still open, and the remaining steps still run. Service references are cleared and logging is closed in every case. Afterwards a single failure is re-raised unchanged (an unterminated Terminal tree stays a `TerminalManagerError`), several as one `ExceptionGroup`. Cancellation and other `BaseException`s are not collected: they end the remaining steps and propagate once references are cleared and logging is closed. Callers need not log a raised failure again: the server lifespan only reports it to the stop line and exit status (`server/process-lifecycle.md` -> `create_app` hooks).
- `aclose()` callers share one cleanup task; caller cancellation propagates only after cleanup settles, and a later call reports the same outcome without running any step again. `start()` after a finished close rebuilds every service.
- Coverage: `test_runtime_lifecycle.py::test_runtime_shutdown_runs_every_step_before_reporting_failures` pins both step orders and the failure contract; `test_failed_start_logs_once_releases_started_resources_and_can_retry` pins failed-startup cleanup. The root `tests/conftest.py` fails a test whose loop-started Runtime is still started or left its performance monitor or a canonical database open.

## Service properties

`suggest_agent_description` wires the stateless Agents-owned operation (`core/agents/description.py`).

Explicit typed service declarations share the read-only readiness/availability guard in `_service_access.py`; special properties retain their individual contracts. Initial construction and every shutdown use the same service-reference initializer, including Extension host groups and the selected Recall integration.

All service properties raise `RuntimeError` outside a started runtime, **except `extensions`** (returns `None`); `chat_runs` is a plain attribute until `start()`. `config` is available pre-start (bind resolution). Non-obvious members beyond self-describing services:

- `provider_credentials` - the central resolver behind `has_provider_credentials`/`get_provider_credentials`; `resolve_environment_credential(key)` resolves env-first without exposing values, `environment_credential_source(key)` reports provenance. The data-directory fallback folds Windows name case by the Secret Store's rule (`secrets.md`, `fold_credential_names`). Web Search, Web Fetch, Extensions and Channels share it, so live reloads reach them all. Confined shells withhold `environment_credential_names()` (`tools/shell.md` -> Environment layers).
- `tools` - `analyze_image` registers but Chat gates its Model-facing visibility on route capability plus ImageService availability; `channel_send` registers dynamically while >=1 valid Agent owns an enabled Channel (adapter liveness irrelevant).
- `file_read_state` - the shared read stamps and mutation locks behind read, the edit Tools and chat `@`-mention snapshots.
- `command_dispatcher` - the canonical stable dispatcher; server/accessor code must reuse `runtime.chat_loop` / `runtime.command_dispatcher` - no probing, no fallback construction (stub runtimes must provide them).
- `build` - the `BuildIdentity` of the running code (`core/utils/version.py`: version, source commit, and release or branch), resolved once at startup because a checkout's HEAD moves on while the process keeps running its startup code. A packaged version reads its `release.json` (`cli/application.md`); a Git checkout, Worktree included, its HEAD files without running Git, where a detached HEAD counts as a release only when the tag `v<version>` points at it. The System Prompt's `{vbot_version}` and `settings.get` `general.build` read it (`tests/core/utils/test_version.py`).
- `performance` - the process `PerformanceService`; instrumented code records through the module-level `core.performance` functions instead of this property (`performance.md`).

The stateless `DecisionService` (no public accessor, no background work, no shutdown step) is passed to the `classify` Tool registration; future Decision-Model Tools register against the same instance (`model_tasks/decisions.md`). `live_voice` (`LiveVoiceService`) holds no background work itself; the server's call registry ends active Live calls in the lifespan before the Runtime closes (`model_tasks/live.md`).

### Hot-reload seams

Reload methods refresh already-wired consumers without restart. Provider/Model registry identities stay stable; Skill, Extension, and Recall registries can be replaced. Provider registry, Skill and credential reloads read on workers and publish on the Event Loop, and only the newest requested generation publishes: a result superseded by another reload or by shutdown/restart is dropped; Extension rebuilds are serialized and last-write-wins under `ExtensionRuntime`. `apply_settings_change` applies all live effects of one validated Settings write; callers keep the mutation serialized through its await. Every reload path: `runtime/hot-reload.md`.

### Provider operations

`core/providers/runtime.py::ProviderRuntime` owns Adapter selection/creation, credential/token binding, Debug/local Context injection, Reasoning descriptions, HTTP pools, catalog refresh/staging/tasks and Connection reachability. Runtime keeps the facade methods, including worker-safe `prepare_provider_http(ConnectionRef)` binding credentials and pools to one Account. HTTP ownership, invalidation and shutdown: `providers/request-policy.md` -> HTTP, retry, and streaming; catalog policy: `providers/catalog-discovery.md`. `ConnectionRef` (`core/providers/accounts.py`) bundles the Provider id and compositional `provider:connection[:account]` id.

### Identity Agent rename

`rename_agent(agent_id, new_agent_id)` is the one entry for renaming an Identity Agent, and Runtime owns its order across every referencing service (Agent store, Channels, Cron, Bootstrap, Terminals, shell commands) as one compound mutation that reverts completely on failure; `agent_references(agent_id)` serves the `agent.delete` guard. Order, threading and revert: `runtime/agent-rename.md`.

## Constraints & Gotchas

- First-start recovery creates a bootstrap Agent (`main`/`Main`, shifting to free `main-N` when an invalid preserved directory occupies the name), with no Session yet: its first message creates one (`agent.md`). Existing directories with any valid Agent are preserved - no second `main`.
- Invalid individual Agent/Project configs skip individually (Bootstrap); Projects need only `project_id` + `cwd`.
- The bootstrap Agent ensures **before** ChannelService starts so a channel targeting `main` recovers on first start. A pending Agent rename finishes its Agent-owned half before the first roster read and that ensure, and moves its references before any reference owner starts (`test_runtime_agent_rename.py`).
- Channel/Cron starts, the usage collector and the performance monitor share the event-loop guard: wired but not started when no loop exists. Channel startup failures isolate to failed health state; Cron storage corruption disables scheduling without failing startup and protects state from overwrite.

## References

Read these only when your task matches - not by default.

- Using or changing `core/utils/workers.py` (`BoundedWorkerPool`, `OrderedWorker`, `settle_before_cancelling`, `finish_despite_cancel`, `cancel_and_wait`) or running blocking work off the Event Loop in any domain -> `runtime/workers.md`
- Using or changing `core/utils/tls.py` (`shared_ssl_context`, `ssl_context_for`, `prewarm_outbound_http`) or creating an outbound HTTP or `wss://` client in any domain -> `runtime/outbound-tls.md`
- Changing a live reload path (`apply_settings_change`, Provider/local catalog/Skill/Recall/credential/Keep Awake/Channel Tool/Extension reloads) -> `runtime/hot-reload.md`
- Changing Runtime's Extension host side (`ExtensionHostFactory`, temporary execution groups, editor catalogs, `inspect_prompt`, `_prompt_blocks.py`, Extension sampling, `resolve_tool_agent`) -> `runtime/extension-host.md`
- Changing the Identity Agent rename or `agent_references` -> `runtime/agent-rename.md`
- Adding, removing or reordering a service in bootstrap or shutdown, or tracing why a phase or step sits where it does -> `runtime/lifecycle-steps.md`
