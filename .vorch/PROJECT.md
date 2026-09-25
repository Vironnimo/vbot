# Project Context

## Project

vBot is a self-hosted environment for working with AI Agents across conversations, Projects, and automated tasks. It brings together persistent Sessions, Agent identity and Memory, project context, and Tools for acting on the host system and connected services. Agents can work directly with the user or carry out tasks independently.

The runtime and shared state live on the server. WebUI, Desktop, CLI, and Channels provide different ways to interact with the same system.

## Architecture

**Stack:** Python 3.11+ (hatchling), FastAPI + WebSocket + SSE, Svelte (JS, no TypeScript), pywebview. Kernel uses asyncio; threads only where native libraries require them.

**Windows application:** `cli/application/` owns per-user packaged versions, independent durable updates, the native `vBot.exe` tray facade and local development candidates. Release and main (`-Dev`) installations share that lifecycle; main uses a recorded Git source to prepare immutable runtime versions. Desktop remains an independent accessor; no Windows service. `scripts/build_windows.py` and `scripts/windows/` assemble private CPython 3.13 for all Windows shapes, readable runtime sources and built assets. Source-only/Linux lifecycle stays in its existing owners. Read `cli.md` and its Windows application reference before changing this boundary.

**Layers:**
```
core/          <- Kernel (async). No HTTP, no UI.
server/        <- FastAPI + WS + SSE. Imports core/. RPC dispatch lives in server/rpc/.
webui/         <- Svelte frontend. Own package.json. Talks HTTP/WS/SSE only.
cli/           <- CLI accessor. Server lifecycle locally; all other domains via shared RPC client.
desktop/       <- pywebview shell. Imports nothing from the project - HTTP only.
```

Bare map references resolve under `.vorch/domain-maps/`. Each `core/<module>/` folder's main file is its public API (soft limit: 1000 lines/file). Exceptions: `core/debug` uses an `__init__.py` facade (`debug.md`); independent utilities in `core/utils` use leaf imports.

Large source files are an independent maintenance problem: they increase the context and tokens needed to inspect and edit code, even when the domain responsibility is cohesive. Split oversized files into focused internal units while retaining the owning module and its public contract. Cohesion alone does not justify leaving a multi-thousand-line file intact; avoid arbitrary chunks, method-binding tables, or compressed formatting that merely obscure the same required context.

**Transport:** Commands use `POST /api/rpc`, never WebSockets. `/ws` carries persistent app-wide server-push events; SSE streams each Run; logs and terminals use dedicated sockets. Binary transfers use dedicated HTTP endpoints. No auth (single-user-local). See `server.md`.

**Flow:** Accessors -> HTTP/WS/SSE -> server RPC handlers -> core (Providers, Models, Tools, Agents) -> external APIs. Agentic-only; no separate non-agentic streaming path.

Live voice is the `live_voice` Task Model: the server owns the provider call, delegated reasoning, and app operations; the accessor holds only WebRTC media to the provider and answers UI requests. See `model_tasks/live.md` for details and secure-context limits.

**Persistence:** Every SQLite database opens through the shared kernel `core/database/` (`database.md`): connection and journal policy, additive schema evolution with a migration ledger, and for canonical databases the data-store marker, data snapshots, quarantine and auto-restore. Canonical Session history: normalized columns in `<data-dir>/sessions.db` (SQLite `STRICT`, WAL where safe - packaged Windows runtimes pin a WAL-safe SQLite - `synchronous=FULL`). `<data-dir>/data-store.json` authorizes every canonical database and records its identity and format generation. External-content FTS indexes searchable entries; a second trigram index covers only User, Assistant and Compaction-checkpoint text. No mirrored search-text table. A fork shares its source's history through lineage instead of copying it (`sessions.md`). Verified data snapshots under `<data-dir>/snapshots/` (every canonical database plus the durable JSON documents except blob sidecars) provide per-database auto-restore and the packaged updater's rollback after a failed candidate; only explicit operator and update workflows create them (the Generation 1 converter keeps replaced files under `pre-generation-1/` instead). Normal Runtime startup/operation never copies a database. Recovery incidents live under `incidents/`, damaged files under `quarantine/`.

**Tools:** Execute a call whose intended effect is clear even when it does not match the schema; refuse only genuinely ambiguous calls, before side effects, with the corrected call. Similarity alone is not intent for a mutation or an explicit target. Schemas, argument normalization/validation, concurrency: `tools.md`. Agent-facing design: `tools/designing-agent-tools.md`; review procedure and instruments: `.vorch/workflows/tool-review-workflow.md`.

**Extensions:** API 8 (`core/extensions/_declarations.API_VERSION`). API 6 added owner-bound temporary Sessions/execution groups and isolated built pages, API 7 owner-bound Extension databases, API 8 Tool result payloads. Canonical Sessions own binding, receipt and Run identity; Extension databases own domain state. The app provides a generic page bridge; Extensions own domain UI (`extensions.md`).

**Configuration:** Data directory `~/.vbot` owns `settings.json` and `.env`, a user-owned fallback credential snapshot. Process environment takes precedence; vBot never rewrites `os.environ`. Owning domains validate every user-editable JSON file before runtime use; public accessors configure Settings only through cataloged paths. See `settings.md`, `storage.md`, and `providers/connections.md` (Custom Provider credentials).

## Domain Maps

Read domain roots and task-relevant references under `.vorch/domain-maps/` as described in `AGENTS.md` -> Load context for the task. Maps orient you to owners, boundaries, contracts, source, and tests. Code establishes implemented behavior; user requirements and engineering contracts establish obligations. Correct stale descriptions without treating existing behavior as the desired outcome (see `AGENTS.md` -> Interpret documentation).

| Map | Domain | Covers |
|---|---|---|
| runtime.md | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| providers.md | `core/providers/` | Provider boundary, Connection/discovery/request/usage invariants |
| models.md | `core/models/` | Model DB layers, registry, capabilities, id convention |
| model_tasks.md | `core/model_tasks/` | Task-model bindings/execution, target discovery, option schemas, Jev experiments and external Actions |
| chat.md | `core/chat/` | ChatMessage boundary, Agentic Loop invariants |
| runs.md | `core/runs/` | Run lifecycle, cancellation, timeline events, queues |
| compaction.md | `core/compaction/` | Triggers, strategies, plans, checkpoints |
| sessions.md | `core/sessions/` | Canonical SQLite Session persistence, metadata, and lifecycle |
| database.md | `core/database/` | Shared SQLite kernel, format-stability contract, data-store marker, data snapshots and recovery |
| recall.md | `core/recall/` | Recall backends: canonical scan, FTS index, vector index |
| statistics.md | `core/statistics/` | Disposable SQLite projection, report RPC |
| memory.md | `core/memory/` | Pinned memory service, workspace memory files |
| settings.md | `core/settings/` | Settings schemas, validation, update sections |
| prompts.md | `core/prompts/` | System Prompt assembly, fragments, variables |
| attachments.md | `core/attachments/` | Blob storage, MIME sniffing, text extraction |
| extensions.md | `core/extensions/` | Extension kernel boundary, loading/lifecycle, management operations, live Tool catalogs, bundled MCP |
| agent.md | `core/agents/` | Agent schema, workspace lifecycle, archive-on-delete |
| projects.md | `core/projects/` | Project boundary, anchor/ceiling invariants |
| subagents.md | `core/subagents/` | Sub-agent coordinator, batch tracking, run linkage |
| tools.md | `core/tools/` | Tool contracts and policy; index to per-tool maps |
| storage.md | `core/storage/` | Data-directory layout, temp-file lifecycle, persistence |
| skills.md | `core/skills/` | Skill loading/validation, scopes, Prompt-Epoch Catalog |
| automation.md | `core/automation/` | Cron/Bootstrap triggering, queue semantics |
| calendar.md | `core/calendar/` | Local events, recurrence, event-relative Agent actions, cron projection, calendar tool |
| channels.md | `core/channels/` | Channel adapters, conversation engine, outbound send |
| model-communication.md | cross-cutting | Sanctioned kernel-to-Model channels; never invent one |
| server.md | `server/` | Transport/RPC boundary, events, source routing |
| cli.md | `cli/` | Server lifecycle commands, targeting, output contract |
| desktop.md | `desktop/` | pywebview shell contract, voice bridge |
| webui.md | `webui/` | Frontend accessor boundary, shared invariants |
| logs.md | log viewer subsystem | Log parsing, RPC/socket contract, Logs tab |
| debug.md | `core/debug/` | Debug Mode, traces, redaction, recorder |
| performance.md | `core/performance/` | Always-on metrics, Event Loop stalls, Perfetto Recordings, metric catalog |

## Conventions

**Dependency injection:** Constructors (`__init__`) and `typing.Protocol` interfaces; no service locator or global singletons. Two exceptions: performance measurement records through module-level functions into one process-wide sink, like logging; it holds only measurements, never state that decides behavior (`performance.md`). Every server `httpx` client or transport, bundled Extensions included, passes `verify=shared_ssl_context()` (`core/utils/tls.py`), and every `websockets` `wss://` connection passes it as `ssl`: one process-wide context with httpx's default verification, prewarmed off the Event Loop at bootstrap, because a bare client or connection re-parses the CA bundle (150-250 ms, blocking) each time; `tests/core/utils/test_tls.py` guards every call site.

**Object IDs:** Use `core/utils/ids.py` for vBot-owned references: short type prefix + 12 lowercase base32 characters (60 random bits) with atomic owner-side uniqueness claims; 16 characters (80 bits) for pre-persistence/high-volume identities lacking a complete allocation catalog (Messages, Runs, Queue items). Storage and Tool results use the same id; no display aliases or abbreviated incoming ids. Preserve existing opaque ids exactly. Path-backed readers validate safe basenames, not generator format; exact-id stores confirm the stored spelling with `has_id_entry` so case-insensitive filesystems cannot alias case variants. Owners authorize access; prefixes/randomness do not. Provider/protocol ids, credentials, hashes, and private storage generations keep their own contracts. Tests: `tests/core/utils/test_ids.py` and owner collision/round-trip tests.

**Errors:** Base classes in `core/utils/errors.py`, domain subclasses per module. Expected errors: handle locally, log `warn`; unexpected: rethrow, log `error`. Never silently swallow. Transient HTTP retries use shared `core/utils/retry.py` backoff and idempotency-aware statuses in `core/utils/http_status.py`. Chat owns ordinary Model retries through one Run-local recovery budget and suppresses nested utility retries for each Model attempt; other callers retain their retry policy. Provider errors are `retryable` or `fatal`.

**Logging:** Structured logs via `LogManager` (`core/utils/logging`), per-module `vbot.<domain>` loggers, `<data_dir>/logs/`. Standalone Desktop uses the same format without importing core logging. No `print()` or `logging.basicConfig()`. Each material control-plane mutation emits one post-change `INFO` event: operation, stable target ids, changed fields. Never log credentials, token values, Provider Account ids, Prompt/Skill/Cron content, or external conversation ids. Reads, polls, appearance changes, acknowledgements, routine traffic, and effective no-ops stay silent. Operational failures and health transitions use `WARNING`/`ERROR`.

**Time:** Persist ISO 8601 UTC timestamps with explicit offset; database tables store the canonical fixed-width form `YYYY-MM-DDTHH:MM:SS.ffffffZ` (`core/utils/timestamps.py`, `database.md`). Optional IANA `timezone` defaults to the server host zone; once Settings resolve, it alone controls Agent context, wall-clock Calendar/Cron behavior, and UI rendering, never implicit browser/host time. No implicit `datetime.now()`.

**Persisted formats are stable (Generation 1).** Durable databases and JSON documents evolve compatibly: additive changes only (new tables, nullable or defaulted columns, optional JSON fields); readers tolerate and writers preserve unknown fields; data backfills are named, idempotent migrations recorded in the database, and a migration that older versions cannot read declares so. Renames, type or meaning changes, removals and new constraints require a new format generation with an explicit converter run by the updater or CLI, never by normal startup. App code knows only the current generation: no fallback keys or old-field branches. Details: `database.md` (SQLite) and `settings.md` -> JSON Document Contract.

**I18n:** All user-visible strings use i18n with English fallback: backend `utils/`, frontend `webui/src/lib/i18n.js`.

**Model-facing paths:** Render separators as `/` when vBot authors a known filesystem-path value for Model context (System Prompt, attachment note, Tool result, delivery note). Leave `pathlib.Path`, native OS calls, persisted values, incoming arguments, and arbitrary text unchanged; no global replacement.

## Development

**Setup:** Python >= 3.11, Node.js for WebUI (Node.js >=22 plus npm on the server for optional WhatsApp Channels); editable install with dev extras:
```bash
pip install -e ".[dev]"
python -m cli.search_runtime
```
Use the current interpreter; do not assume a virtual environment for installs, gates, or runtime commands. Before editing installer/uninstall scripts in `scripts/`, read [USAGE.md](../USAGE.md#installation) for end-user installation/update/removal.
The development extra includes the native tray dependency on Windows so fresh local and CI environments can run its platform-specific tests. `cli.search_runtime` provisions the private, digest-locked ripgrep/PCRE2 executable; setup, update, worktree creation, CI, and server application packaging perform this step. Tool calls never download an executable or fall back to a host-installed search engine.

**Worktrees:** `python scripts/worktree.py create|list|merge|delete <task-name>`; also `repair-start|repair-finish`. `create` reports path, ports, data dir, URL; it reserves `dev` and refuses an existing data root. Cleanup stops services and removes data only with matching ownership records; older worktrees keep their unverified data, reported in the output. Non-force `delete` fails closed on Git removal errors unless `git worktree list` confirms deregistration; a leftover directory without `.git` holds no work, so both modes finish it (branch read from Git's registration). `delete --force` discards uncommitted work. `merge` lands the task branch on `main` and removes the worktree, with a merge lock and protected conflict-repair window. The agent must pass quality gates before merging; this tool never runs them. On failure/unexpected behavior, read `scripts/README-worktree.md`.

**Dependencies:** `pyproject.toml` groups include `server`, `cli`, `windows-app`, `desktop`, `local-speech`, `local-tts`, and `dev`; frontend: `webui/package.json`. `core/model_tasks/speech_setup.py` owns optional speech setup. Packaged Windows STT/TTS use managed user-data environments and child workers; source-checkout STT retains its server-interpreter recipe. No optional setup mutates a packaged release. `psutil` provides verified process-tree cleanup and server restart support. `sniffio` is a direct dependency although vBot never imports it: httpcore probes it on every connection and stream, and without it each probe pays a failed import and `sys.path` scan (~0.4 ms of GIL time). See `model_tasks/speech.md` and `USAGE.md` -> Local speech recognition / synthesis.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```
A git-ignored checkout marker selects dev data `~/.vbot-dev`, port `8421`. Installed CLI outside the checkout uses product defaults `~/.vbot`, `8420`. Never target the installed instance with development commands, including its interpreter: running an installed `versions/<id>/runtime/python.exe` writes `__pycache__` into the verified version. Current versions remove such caches before verifying (`cli/windows-application.md` -> Update operation); older active versions fail the next update with "Release file inventory does not match its payload". Managed worktrees have separate data dirs and ports.

**Data store:** Live operator-safe health of every canonical database: `python cli/main.py data-store status|snapshot|incident|unregister`; `snapshot restore` requires a proven-stopped target, and `unregister` releases a removed Extension's database. Session Runs, Messages, Tool invocations/results and checkpoints are stored relationally in `sessions.db`. A data directory from before Generation 1 (the 0.4.x releases, `~/.vbot-dev` included) is converted once, offline, with `python -m scripts.converters.persistence_generation_1 <data-dir> [--dry-run]`; procedure, `pre-generation-1/` and interruption: `database/generation-1-conversion.md`.

**Frontend build:** `cd webui && npm ci && npm run build`. Also compiles bundled Extension `ui/page.html` entries to relative `web/` assets via `webui/scripts/build-extension-pages.mjs`; installers ship assets and Extension sources. The frontend gate covers these external source/test paths with the shared dependency tree.

**Release:** Read `.vorch/workflows/release-workflow.md` when the user requests a release.

Windows binary artifacts require configured release signing, and publication is separate from building/testing. Unsigned developer packages are explicitly selected with `--package`; they are never accepted as unsigned official downloads. Local customization needs Git and Node.js/npm only in its development copy, not during normal packaged operation, except the optional WhatsApp connection which currently requires an operator-installed Node.js >=22 and npm. Tests of Windows application lifecycle use disposable install/data roots, never the existing installed instance.

## Testing

Backend: pytest with `--import-mode=importlib`; frontend: Vitest, optionally jsdom when helper assertions cannot cover rendered components. Tests mirror source: `tests/<package>/<module>/test_<file>.py` and `webui/src/<module>/__tests__/`.

**Text assertions:** Exact strings only for stable contracts (protocol tokens, persisted formats, accessibility names, forbidden internal values) or test-owned transport sentinels. For editable prose, errors, and help, assert exception types, codes, structured fields, DOM roles, or security invariants instead. Evaluate wording in scenarios, not substring tests.

**Event Loop isolation:** Async tests and fixtures on a worker share one session-scoped Event Loop, so a task a test leaves behind keeps running during later tests. Close every `Runtime` started inside a test's Event Loop with `await runtime.aclose()`; the root `tests/conftest.py` fails a test that leaves one running. Never patch the process-wide `asyncio.sleep`: a module whose waits tests skip exposes a module-local `_sleep` seam (for example `core.utils.retry._sleep`), and tests patch that seam.

**Quality gates:** `scripts/quality.py` (backend) and `scripts/quality-frontend.py` share format -> lint -> type-check -> test, scoped by paths or full with none. Use `--check` for non-mutating development/CI feedback; before commits use scoped auto-fix mode and keep every fix, so tests cover the fixed code. Include affected callers/tests explicitly; mapping misses cross-domain dependencies. Use full gates for broad or unscopable effects. Frontend pre-commit requires `--build`: whole WebUI build, unchanged lint/test scope. Use gates, not direct pytest/ruff/vitest; record suspected gate omissions in `.vorch/FLAGGED.md`. Pipeline, test mapping, and output details: `scripts/README-quality.md`.
```bash
python scripts/quality.py <paths...>                  # Backend pre-commit
python scripts/quality.py --check <paths...>          # Backend feedback
python scripts/quality.py --check --profile           # Full check + 25 slowest tests
python scripts/quality-frontend.py --build <paths...>  # Frontend pre-commit
python scripts/quality-frontend.py --check <paths...>  # Frontend feedback
# Omit paths for full gates on the affected side(s).
```

**Performance:** Manual, outside the gates. `python scripts/perf_load.py` (concurrent-Agent load test on a disposable server with a scripted fake Provider), `python scripts/perf_bench.py` (hot-path microbenchmarks), and the always-on server metrics/Recordings (`vbot performance`, `performance.md`). Prove optimizations with `--compare` against a baseline from the same machine; results stay in the git-ignored `perf-results/`. Usage and interpretation: `scripts/README-perf.md`.

## Live Testing

Before live tests, fully read `.vorch/workflows/web-test-workflow.md` for browser WebUI testing, `.vorch/workflows/cli-test-workflow.md` for CLI testing; both for tasks spanning both accessors.

## End-to-End Testing

Playwright `tests/e2e/` is excluded from both quality gates; release CI requires it before publishing (`.github/workflows/e2e.yml`). Local runs require explicit user request and a full read of `.vorch/workflows/e2e-test-workflow.md` before every run.

## Context

Only strategic decisions or global constraints an Agent might otherwise misread belong here.

- **Platforms:** vBot targets Windows and Linux, with Windows currently prioritized. Keep shared components portable and platform-specific integration isolated. Server and accessors may run on the same machine or separate hosts.
- **Durable JSON documents:** every JSON document an owner keeps in the data directory as durable state (configuration, operator state, stored metadata such as blob sidecars) follows the Generation 1 contract in `settings.md` -> JSON Document Contract (`format_version`, unknown fields kept, no overwrite after a failed load). It joins through `core.json_documents.DURABLE_DOCUMENTS` plus a `doctor config` validator, and is thereby a data-snapshot member unless listed in `DOCUMENTS_OUTSIDE_SNAPSHOTS` (blob sidecars: snapshots hold no blobs). Only these JSON files stay outside it (listed there): kernel files of `core/database/`, the refreshed Model DB catalog (its own manifest, bundled fallback), install receipts and markers, diagnostics, and transient runtime state that exists only while an operation is in flight and fails safe when unreadable (Cron once-fire claims).
- **Kernel-to-Model notifications:** Only sanctioned channels from `model-communication.md`: persisted notes rendered as System Reminders, System Prompt blocks, Tool definitions/results. Every domain (including Extensions, Channels, Tools, automation) must use these; never invent a channel.
