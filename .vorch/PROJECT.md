# Project Context

## Project

vBot is a local-first agent harness — a runtime that gives agents maximum agency with minimal restrictions. A single async Python kernel powers four accessors: a FastAPI server, a Svelte web UI, a pywebview desktop shell, and a CLI.

Agents are first-class citizens with tool access to the host system. They can read and edit the application source (self-healing — fixing bugs they encounter during their work, or adding small features on the fly), configure the system via the CLI (set up Telegram channels, add API providers, switch the agent's model, etc.), and trigger application restarts to apply changes. The agent lives where the server lives; desktop and CLI are accessors.

This is a technical-user tool. The agent has the same capabilities as the user, with a small set of critical guardrails.

## Architecture

**Tech stack:** Python 3.11+ (hatchling), FastAPI + WebSocket + SSE, Svelte (JS, no TypeScript), pywebview. Async-first — asyncio throughout the kernel, threads only where native libraries force them.

**Layers:**
```
core/          ← Kernel (async). No HTTP, no UI.
server/        ← FastAPI + WS + SSE. Imports core/. RPC dispatch lives in server/rpc/.
webui/         ← Svelte frontend. Own package.json. Talks HTTP/WS/SSE only.
cli/           ← CLI accessor. Server lifecycle locally; all other domains via shared RPC client.
desktop/       ← pywebview shell. Imports nothing from the project — HTTP only.
```

**Core modules:** runtime, models, model_tasks, chat, runs, compaction, sessions, recall, statistics, memory, settings, prompts, attachments, extensions, agents, subagents, tools, providers, channels, skills, automation, storage, utils. Each is a folder with a main file as public API, soft limit 1000 lines per file. `model_tasks/` is the single deep task module: it owns specialized task-model bindings and target discovery (`model_tasks.py` as the main file) **and** the per-task execution services with their provider wire clients (`speech*.py`, `image*.py`, `embeddings*.py`). Provider and automation internals live in their domain maps (`providers.md`, `automation.md`).

**Communication:** `POST /api/rpc` (method dispatcher) + `/ws` (event-bus push) + `/ws/logs` (selected log-file live tail) + SSE (streaming) + dedicated attachment HTTP endpoints (`POST /api/upload`, `GET /api/attachments/{id}`). No auth (single-user-local).

**Data flow:** Accessors → HTTP/WS/SSE → server RPC handlers → core (orchestration via providers, models, tools, agents) → external APIs. Agentic-only — no separate non-agentic streaming path.

**Configuration:** `settings.json` for application settings, `.env` for API keys and bot tokens. Both live in the data directory (`~/.vbot`). The `.env` belongs to the user and is read at startup as a fallback credential source; process environment keeps higher precedence than the data-dir `.env`, and vBot never rewrites `os.environ` from `.env` values. Settings read-modify-write is serialized through a process-local storage transaction and persisted with one atomic JSON replace; `settings.update` applies all accepted sections in one transaction before any runtime reload hooks run. Every user-editable JSON format is validated by its owning domain before runtime code consumes it: Settings owns `settings.json`, Agents owns `agents/*/agent.json`, Channels owns `channels/*/channel.json`, Projects owns `projects/*/project.json`, and Automation owns `cron/jobs.json`. They share transport-neutral file parsing and diagnostics through `core/config_validation.py`; Settings orchestrates the whole bundle for `vbot doctor config` without owning the other schemas. Individual `settings.json` keys and update sections are documented in `.vorch/domain-maps/settings.md`.

**I18n:** Every user-visible string through the i18n system from day 1. English fallback. Backend: `utils/`, Frontend: `webui/src/lib/i18n.js`.

## Domain Maps

Each domain has a **domain map** in `.vorch/domain-maps/`, named after its module. A **domain** is any module or subsystem with a clear boundary that you need context about before touching it. A domain map is factual working notes to orient you before you touch the domain — not the ultimate source of truth: when a map and the code disagree, the code wins, and you fix the map. **When you work on a domain, read its map.** Your task lists the relevant maps as a starting point, not a ceiling — read others if you need them.

| Map file | Domain | What it covers |
|---|---|---|
| `.vorch/domain-maps/runtime.md` | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| `.vorch/domain-maps/providers.md` | `core/providers/` | Provider boundary and shared invariants with task-gated Connection, discovery, request, usage, and integration references |
| `.vorch/domain-maps/models.md` | `core/models/` | Model data classes, registry, capabilities, model ID convention |
| `.vorch/domain-maps/model_tasks.md` | `core/model_tasks/` | Specialized task-model bindings, target discovery, option schemas; index to the task-execution child maps |
| `.vorch/domain-maps/model_tasks/speech.md` | speech execution | Speech-to-text and text-to-speech execution, artifacts, provider wire behavior |
| `.vorch/domain-maps/model_tasks/image.md` | image execution | Image generation execution, artifacts, provider wire behavior |
| `.vorch/domain-maps/model_tasks/embeddings.md` | embedding execution | Text-embedding execution, provider wire, vector output for recall |
| `.vorch/domain-maps/chat.md` | `core/chat/` | Canonical ChatMessage boundary, Agentic Loop invariants, and task-reference routing |
| `.vorch/domain-maps/runs.md` | `core/runs/` | Run lifecycle, cancellation, timeline events, in-memory queues |
| `.vorch/domain-maps/compaction.md` | `core/compaction/` | Policy-driven Triggers, Strategies, Plans, and self-contained checkpoints |
| `.vorch/domain-maps/sessions.md` | `core/sessions/` | Session persistence, metadata, current JSONL storage contract |
| `.vorch/domain-maps/recall.md` | `core/recall/` | Session recall backend interface, JSONL scan backend, SQLite FTS derived index, vector chunked semantic index |
| `.vorch/domain-maps/statistics.md` | `core/statistics/` | Read-only on-demand aggregation over Sessions, run-summary segmentation, real-vs-estimated tokens, `statistics.report` RPC, Statistics tab |
| `.vorch/domain-maps/memory.md` | `core/memory/` | Pinned memory service, workspace memory files, backend boundary |
| `.vorch/domain-maps/settings.md` | `core/settings/` | Public settings update schemas, validation, section normalization, parser errors |
| `.vorch/domain-maps/prompts.md` | `core/prompts/` | System Prompt assembly, editable fragments, prompt variables |
| `.vorch/domain-maps/attachments.md` | `core/attachments/` | Blob storage, MIME sniffing, attachment metadata, text extraction |
| `.vorch/domain-maps/extensions.md` | `core/extensions/` | Extension kernel boundary, loading and lifecycle invariants, capability/management reference routing |
| `.vorch/domain-maps/agent.md` | `core/agents/` | Agent schema, persistence, workspace lifecycle, archive-on-delete |
| `.vorch/domain-maps/projects.md` | `core/projects/` | Project boundary, anchor and ceiling invariants, and task-gated configuration, scanning, and resolution references |
| `.vorch/domain-maps/subagents.md` | `core/subagents/` | Sub-agent coordinator, in-memory batch tracking, parent-child run linkage |
| `.vorch/domain-maps/tools.md` | `core/tools/` | Tool domain overview and index to tool-specific maps |
| `.vorch/domain-maps/storage.md` | `core/storage/` | Data-directory setup, settings persistence, prompt fragments |
| `.vorch/domain-maps/skills.md` | `core/skills/` | Skill loading/validation, agent/project/global/bundled scopes, the validated authoring write core + write-scope boundary, origin-grouped session-pinned catalog |
| `.vorch/domain-maps/automation.md` | `core/automation/` | Programmatic run triggering and in-memory queue semantics |
| `.vorch/domain-maps/channels.md` | `core/channels/` | Channel configs, adapter lifecycle, shared conversation engine, metadata, outbound send; index to channel-specific maps |
| `.vorch/domain-maps/server.md` | `server/` | Transport/RPC boundary, public invariants, source routing, and event-reference index |
| `.vorch/domain-maps/cli.md` | `cli/` | Local server lifecycle + `desktop` GUI-launch commands, targeting rules, status/logging contract |
| `.vorch/domain-maps/desktop.md` | `desktop/` | pywebview thin-client contract, in-window connection screen, remembered servers, native menu, per-user config, voice bridge |
| `.vorch/domain-maps/webui.md` | `webui/` | Svelte accessor boundary, shared frontend invariants, and task-specific reference routing |
| `.vorch/domain-maps/logs.md` | log viewer subsystem | Daily log parsing, log RPC/socket contract, WebUI Logs tab behavior |
| `.vorch/domain-maps/debug.md` | `core/debug/` | Debug Mode, trace storage, secret redaction, recorder lifecycle, debug RPC contract |

## Conventions

**Deep modules — few, large, simple interface:** We want few deep modules, not many shallow ones. A deep module hides a lot of functionality behind a simple interface.

**Dependency injection:** Constructor injection via `__init__`. Interfaces via `typing.Protocol`. No service locator, no global singletons, no `getattr` tricks.

**Error handling:** Base classes in `core/utils/errors.py`, domain-specific extensions per module. Expected errors → handle locally, log `warn`. Unexpected errors → rethrow, log `error`. Transient HTTP errors → max 3 retries, exponential backoff + jitter, honoring a server `Retry-After` hint as a floor (capped). Which HTTP statuses are retryable is defined once in `core/utils/http_status.py` (`is_retryable_status`, idempotency-aware), shared by providers and HTTP tools. Provider errors classified as `retryable` vs `fatal`. No silent `except Exception: pass`.

**Logging:** Structured logging via `LogManager` from `core/utils/logging`. All application logs go through that pipeline and use per-module `vbot.<domain>` loggers. Required format: `timestamp [LEVEL] name - message`. Logs live under `<data_dir>/logs/`; `LogManager` handles the file layout. No `print()`, no `logging.basicConfig()`, and no ad-hoc formatting.

**Naming:** Descriptive, no abbreviations (except `id`, `url`, `db`). One thing per function, max 3 nesting levels.

**Imports:** stdlib → third-party → local. Blank line between groups. Remove unused.

**Time:** Persisted timestamps in UTC with explicit offset (ISO 8601). UI renders in user timezone. No implicit `datetime.now()`.

**No legacy compatibility in app code — ever.** We are in development; schemas and config formats can and will break. The app reads the current format and nothing else. No auto-migrations, no fallback keys, no "if old_field then…" branches in application code. If a format changes, the old version is simply invalid. Manual conversion scripts go in `scripts/converters/` — they are standalone tools run explicitly by the user, not hooked into app startup or storage layers.

**Frontend:** Svelte with JavaScript (no TypeScript). All user-visible strings through i18n — no hardcoded text.

## Development

**Prerequisites:** Python >= 3.11, Node.js (for webui).

**Setup:** For development, install the editable package with dev extras:
```bash
pip install -e ".[dev]"
```
Use the current Python interpreter directly — do not assume a virtual environment for installs, quality gates, or runtime commands. End-user installation (the one-line bootstrap, the Windows/Linux installers with autostart and desktop add-ons, update, and uninstall) lives in [USAGE.md](../USAGE.md#installation); read it only when you touch the installer or bootstrap scripts under `scripts/`. Successful installers persist checkout-local lifecycle state in `.vbot-install.json`; `cli/install_state.py` owns its schema and atomic I/O, and update/uninstall use its recorded dependency groups and Python interpreter instead of inferring the environment on every run. The file is git-ignored and contains no runtime data or credentials.

**Worktree commands:** Project worktrees are managed with:
```bash
python scripts/worktree.py create <task-name>
python scripts/worktree.py list
python scripts/worktree.py delete <task-name> [--force]
```
`create` prints the worktree `path`, assigned `port`, data dir, and URL. `delete --force` discards uncommitted worktree changes. If worktree commands fail or behave unexpectedly, read `scripts/README-worktree.md`.

**Dependency groups:** `server`, `cli`, `desktop`, `dev`. Core dependencies plus each group's extras are declared in `pyproject.toml`; the WebUI's are in `webui/package.json`. See those files for exact packages and versions.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```

**Build frontend:**
```bash
cd webui && npm ci && npm run build   # Svelte → static JS/CSS
```

**Releasing:** When the user wants to release a new version, read `.vorch/workflows/release-workflow.md`.

**Data directory:** `~/.vbot` — created on first run. Holds `.env` and `settings.json` (see Configuration above) plus all runtime data: attachments, logs, sessions, the recall index, cron jobs, per-process shell logs, and prompt overrides. Per-subdirectory layout lives in the relevant domain maps.

## Testing

**Framework:** pytest (backend), Vitest (frontend). Backend pytest uses `--import-mode=importlib` so mirrored test modules may share basenames without collection collisions. Frontend rendered-component tests may use `jsdom` via Vitest when helper-level assertions are not enough.

**Structure:** Tests mirror source. Backend: `tests/<package>/<module>/test_<file>.py`. Frontend: `webui/src/<module>/__tests__/` mirroring source (e.g. `src/lib/__tests__/` for library tests, `src/components/__tests__/` for component tests).

**Pattern:** AAA. Independent, deterministic, no shared state.

**Quality gates:** Two scripts with the same interface — `quality.py` (backend) and `quality-frontend.py` (frontend) — each runs format → lint → type-check → test (→ build for frontend) over the paths you pass, or the whole repo with no args. They auto-fix what they can, map each source file to its mirrored test file(s), and filter tool noise down to an agent-readable verdict (auto-fixed files per step, forwarded failure output, a final verdict line). **These gates are the contract — prefer them over invoking `pytest`/`ruff`/`vitest`/etc. by hand.** Reach for a raw tool only when you genuinely suspect the gate withheld something you need (a filtered-away failure, a test that didn't get mapped); when that happens, append a note to `.vorch/FLAGGED.md` so the gate can be improved to surface it, rather than letting hand-invocation become the habit. Full mechanics — the pipeline, the source→test mapping rules, the output contract, and where to fix a gate gap — live in `scripts/README-quality.md`.
```bash
python scripts/quality.py [paths...]           # Backend
python scripts/quality-frontend.py [paths...]  # Frontend
```
```bash
python scripts/quality.py                          # full backend
python scripts/quality.py core/runtime/            # one module
python scripts/quality.py core/utils/config.py     # single file
python scripts/quality.py core/utils/config.py core/utils/errors.py   # multiple files
```
Frontend script works the same way.

## Live Testing

Live testing means starting the running app and verifying behavior via CLI, API, and browser — not writing unit or integration tests (that is the Builder's job with pytest/Vitest). The Tester agent owns live testing.

All project-specific live testing instructions — startup, health check, browser strategy, which features need API credentials, shutdown — live in **`.vorch/TESTER.md`**. The Tester agent reads this file on every session.

## Context

Use this section only for important strategic decisions, unusual global constraints, or things an agent would otherwise likely assume incorrectly.

- **CLI is an accessor, not a second control plane.** Only `server start`, `server stop`, `server restart`, `server status`, and the local `desktop` GUI launch act locally (alongside `update`/`autostart`/`doctor`). `vbot desktop [--host] [--port]` opens the pywebview window pointed at a local or remote server and builds no `ServerInstance`. Every other CLI area must use server RPC instead of reading or mutating files directly. CLI output is agent-facing: success, failure, help text, and suggestions must be explicit enough for an agent to choose the next command without guessing.
- **Two times, one rule (the Model DB).** *Refresh* fetches (provider `/models` + the public models.dev `catalog.json` + provider task-capability catalogs like OpenRouter's image API) and projects per file to disk — dumb, needs net/key, rare. *Load* assembles each effective model in memory from up to three layers — smart, no net/key, frequent. The rule: a hand-edit to an override takes effect on the next **load**, not on a refresh (override files are read at load time). Details in `.vorch/domain-maps/models.md`.
- **Generated catalogs are refreshable; overrides and the canonical layer are durable inputs.** `resources/models/<provider>.json` is regenerated by refresh (don't hand-edit it). The canonical base `resources/models/models.json` is a refreshable projection of models.dev; `resources/models/models.overrides.json` and `resources/models/<provider>.overrides.json` are hand-maintained input layers, never written by refresh, applied at load. A discoverable provider fact belongs in adapter normalization/runtime policy; a durable, externally-verified fact the feeds don't expose belongs in the matching override layer.
- **Two-channel transport architecture:** SSE is the per-Run streaming channel; WebSocket is persistent app-wide server-push for lifecycle summaries. Clients send commands through `POST /api/rpc`, not through WebSocket.
- **System reminders are kernel-internal notes.** Chat sessions may persist `role: "note"` entries for background events. The chat loop embeds them into provider requests as synthetic user messages wrapped in `<system-reminder>` tags; provider adapters must never receive `role: "note"`, and the normal UI should not present notes as user messages. Visible chat turns can also carry `input_origin: "speech_transcription"` through RPC; the chat loop then adds a hidden system-reminder note immediately before the unchanged visible user message so the model knows the text may contain STT errors.
- **Reply-surface awareness is append-only Session history, not routing state.** Interactive WebUI/Desktop and Channel Runs may carry a Chat-owned surface value. Chat appends a tagged direct System Reminder only for the first surface, a surface switch, or the first interactive Run after a newer Compaction checkpoint; queued items decide when they execute. Channel creation/linking, proactive sends, background Runs, delivery targets, and the System Prompt do not define or mutate this state.
- **Built-in commands and skill triggers are separate layers.** A string or exactly one `TextBlock` may be prepared as a Built-in Command before a normal Run; `/skill-name` and `$skill-name` remain Skill activation hints that preserve the original user message (`$` autocomplete is Skill-only). `CommandDispatcher` owns every Built-in Command end to end and returns surface-neutral feedback, navigation, Runs, and resource changes; RPC and Channels only validate/address ingress and project that result. The command framework and full roster live in `.vorch/domain-maps/chat/commands.md`; the automatic background reflection cadence lives in `.vorch/domain-maps/automation.md`.
- **Deployment target is Linux, development happens on Windows.** The server is meant to run headless on a Raspberry Pi (64-bit OS); desktop/CLI accessors stay on Windows. Keep core/server/cli code platform-neutral: no Windows-only assumptions without a POSIX branch, path validation accepts/rejects both path flavors on any host, and process management branches on `os.name`/`sys.platform`.
- **Busy-session queueing is owned by `ChatRunManager`.** Browser sends, `TriggerService`, and subagent routing all enqueue into the same in-memory FIFO per `(project_id, agent_id, session_id)` — the project anchor is part of the run/queue key because session ids may be caller-chosen and repeat across anchors. WebUI queue state is only a server-backed projection and must not become a second source of truth.
- **Rooting is explicit and separate from Session ownership.** An Identity Agent's nullable `root_project_id` selects the working Project; admitted work snapshots it as internal `working_project_id`. `project_id` remains the Session/address anchor (`None` for every Identity Agent), Workspace remains identity/Memory state, and Workspace/cwd path equality has no meaning.
