# Project Context

## Project

vBot is a local-first agent harness - a runtime that gives agents maximum agency with minimal restrictions. A single async Python kernel powers four accessors: a FastAPI server, a Svelte web UI, a pywebview desktop shell, and a CLI.

Agents are first-class citizens with tool access to the host system. They can read and edit the application source (self-healing - fixing bugs they encounter during their work, or adding small features on the fly), configure the system via the CLI (set up Telegram channels, add API providers, switch the agent's model, etc.), and trigger application restarts to apply changes. The agent lives where the server lives; desktop and CLI are accessors.

This is a technical-user tool. The agent has the same capabilities as the user, with a small set of critical guardrails.

## Architecture

**Tech stack:** Python 3.11+ (hatchling), FastAPI + WebSocket + SSE, Svelte (JS, no TypeScript), pywebview. Async-first - asyncio throughout the kernel, threads only where native libraries force them.

**Layers:**
```
core/          <- Kernel (async). No HTTP, no UI.
server/        <- FastAPI + WS + SSE. Imports core/. RPC dispatch lives in server/rpc/.
webui/         <- Svelte frontend. Own package.json. Talks HTTP/WS/SSE only.
cli/           <- CLI accessor. Server lifecycle locally; all other domains via shared RPC client.
desktop/       <- pywebview shell. Imports nothing from the project - HTTP only.
```

Bare `<name>.md` references throughout this file resolve against `.vorch/domain-maps/`. Each `core/<module>/` is a folder whose main file is the public API (soft limit 1000 lines per file); two recorded exceptions: `core/debug` exposes its API through an `__init__.py` facade (`debug.md`), and `core/utils` is intentionally imported at leaf paths because it bundles independent utilities.

**Communication:** Commands go through `POST /api/rpc`; `/ws` is the persistent app-wide server-push event bus and SSE the per-Run streaming channel. High-volume streams (logs, terminals) use dedicated sockets; clients never send commands over WebSockets. Binary upload/download uses dedicated HTTP endpoints. No auth (single-user-local). Details in `server.md`.

**Data flow:** Accessors -> HTTP/WS/SSE -> server RPC handlers -> core (orchestration via providers, models, tools, agents) -> external APIs. Agentic-only - no separate non-agentic streaming path.

**Persistence:** Canonical Session history lives in `<data-dir>/sessions.db` (SQLite `STRICT`, WAL where safe, `synchronous=FULL`, marker `session-store.json` authorizes creation). `message_search`/`messages_fts` is derived FTS inside the same DB (trigram fallback, stale-breadcrumb, chunked backfill). Verified snapshots in `<data-dir>/session-snapshots/` provide auto-restore; `session-recovery.json` records incidents.

**Tools:** Canonical schema contracts, argument normalization/validation, concurrency policy, and authoring rules live in `tools.md`; Agent-facing definitions follow repository-root `TOOLS.md`.

**Configuration:** The data directory (`~/.vbot`) owns `settings.json` (application settings) and `.env` (user-owned fallback credential snapshot; process environment takes precedence, vBot never rewrites `os.environ`). Every user-editable JSON file is validated by its owning domain before runtime consumption; public accessors configure Settings only through cataloged paths. Contracts and internals live in `settings.md` and `storage.md`; Custom Provider credentials in `providers/connections.md`.

## Domain Maps

Every domain has a map under `.vorch/domain-maps/`. **Read a domain's map before you touch anything in that domain (or when talking to the user about a domain) - without exception.** The map is the briefing on boundaries, contracts, and gotchas you cannot infer from file names; when ownership or contracts cross domains, read the adjacent maps as well. A map orients and guards boundaries - it never documents the code completely, so always read the actual source you are changing alongside it. A map's References are task-gated: pull a supplementary file only when your task matches its trigger. Maps are working notes, not the source of truth: when a map and the code disagree, the code wins - fix the map.

| Map | Domain | Covers |
|---|---|---|
| runtime.md | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| providers.md | `core/providers/` | Provider boundary, Connection/discovery/request/usage invariants |
| models.md | `core/models/` | Model DB layers, registry, capabilities, id convention |
| model_tasks.md | `core/model_tasks/` | Task-model bindings, target discovery, option schemas |
| chat.md | `core/chat/` | ChatMessage boundary, Agentic Loop invariants |
| runs.md | `core/runs/` | Run lifecycle, cancellation, timeline events, queues |
| compaction.md | `core/compaction/` | Triggers, strategies, plans, checkpoints |
| sessions.md | `core/sessions/` | Canonical SQLite Session persistence, metadata, migration, and lifecycle |
| recall.md | `core/recall/` | Recall backends: canonical scan, FTS index, vector index |
| statistics.md | `core/statistics/` | Disposable SQLite projection, report RPC |
| memory.md | `core/memory/` | Pinned memory service, workspace memory files |
| settings.md | `core/settings/` | Settings schemas, validation, update sections |
| prompts.md | `core/prompts/` | System Prompt assembly, fragments, variables |
| attachments.md | `core/attachments/` | Blob storage, MIME sniffing, text extraction |
| extensions.md | `core/extensions/` | Extension kernel boundary, loading/lifecycle invariants |
| agent.md | `core/agents/` | Agent schema, workspace lifecycle, archive-on-delete |
| projects.md | `core/projects/` | Project boundary, anchor/ceiling invariants |
| subagents.md | `core/subagents/` | Sub-agent coordinator, batch tracking, run linkage |
| tools.md | `core/tools/` | Tool contracts and policy; index to per-tool maps |
| storage.md | `core/storage/` | Data-directory layout, temp-file lifecycle, persistence |
| skills.md | `core/skills/` | Skill loading/validation, scopes, Prompt-Epoch Catalog |
| automation.md | `core/automation/` | Cron/Bootstrap triggering, queue semantics |
| calendar.md | `core/calendar/` | Local calendar store, recurrence, cron projection, calendar tool |
| channels.md | `core/channels/` | Channel adapters, conversation engine, outbound send |
| model-communication.md | cross-cutting | Sanctioned kernel-to-Model channels; never invent one |
| server.md | `server/` | Transport/RPC boundary, events, source routing |
| cli.md | `cli/` | Server lifecycle commands, targeting, output contract |
| desktop.md | `desktop/` | pywebview shell contract, voice bridge |
| webui.md | `webui/` | Frontend accessor boundary, shared invariants |
| logs.md | log viewer subsystem | Log parsing, RPC/socket contract, Logs tab |
| debug.md | `core/debug/` | Debug Mode, traces, redaction, recorder |

## Conventions

**Dependency injection:** Constructor injection via `__init__`. Interfaces via `typing.Protocol`. No service locator, no global singletons.

**Error handling:** Base classes in `core/utils/errors.py`, domain-specific extensions per module. Expected errors -> handle locally, log `warn`; unexpected errors -> rethrow, log `error`. Transient HTTP errors retry up to 3 times with exponential backoff + jitter, honoring `Retry-After` as a capped floor; retryable statuses are defined once in `core/utils/http_status.py` (idempotency-aware). Provider errors classify as `retryable` vs `fatal`. Never silently swallow.

**Logging:** Structured logging through `LogManager` (`core/utils/logging`) with per-module `vbot.<domain>` loggers under `<data_dir>/logs/`; the standalone Desktop process attaches the same format without importing core logging. No `print()`, no `logging.basicConfig()`. A material control-plane mutation emits one `INFO` event after the state change (operation, stable target ids, changed fields); never log credentials, token values, Provider Account ids, Prompt/Skill/Cron content, or external conversation ids. Reads, polls, appearance changes, acknowledgements, routine traffic, and effective no-ops stay silent; operational failures and health transitions use `WARNING`/`ERROR`.

**Time:** Persisted timestamps in UTC with explicit offset (ISO 8601). UI renders in user timezone. No implicit `datetime.now()`.

**No legacy compatibility in app code - ever.** We are in development; schemas and config formats can and will break. The app reads the current format and nothing else. No auto-migrations, no fallback keys, no "if old_field then..." branches in application code. If a format changes, the old version is simply invalid. Manual conversion scripts go in `scripts/converters/` - standalone tools run explicitly by the user, never hooked into app startup or storage layers.

**I18n:** Every user-visible string through the i18n system. English fallback. Backend `utils/`, frontend `webui/src/lib/i18n.js`.

**Model-facing filesystem paths use forward slashes.** Whenever vBot authors a known filesystem-path value for Model context (System Prompt, attachment note, Tool result, delivery note), render only its separators as `/` at that boundary. Keep `pathlib.Path`, native OS calls, persisted values, incoming arguments, and arbitrary text unchanged; never apply a global replacement.

## Development

**Setup:** Python >= 3.11, Node.js (for webui). Install editable with dev extras:
```bash
pip install -e ".[dev]"
```
Use the current interpreter directly - do not assume a virtual environment for installs, gates, or runtime commands. End-user install/update/uninstall lives in [USAGE.md](../USAGE.md#installation); read it when touching installer or uninstall scripts under `scripts/`.

**Worktrees:** Managed with `python scripts/worktree.py create|list|merge|delete <task-name>` plus `repair-start|repair-finish`. `create` prints the worktree path, assigned ports, data dir, and URL; `delete --force` additionally discards uncommitted worktree changes; `merge` lands the finished task branch on `main` and removes the worktree, serializing concurrent merges through a lock with a protected repair window for conflict resolution. The tooling never runs quality gates — green gates before merging stay with the agent. If anything fails or behaves unexpectedly, read `scripts/README-worktree.md`.

**Dependencies:** Groups `server`, `cli`, `desktop`, `dev` in `pyproject.toml`; the WebUI's in `webui/package.json`.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```
This checkout carries a git-ignored marker selecting the dev data directory (`~/.vbot-dev`, port `8421`); an installed CLI outside the checkout keeps product defaults (`~/.vbot`, `8420`). Never point development commands at the installed instance. Managed worktrees use their own data dirs and ports.

**Build frontend:** `cd webui && npm ci && npm run build`

**Releasing:** When the user wants to release a version, read `.vorch/workflows/release-workflow.md`.

## Testing

pytest backend, Vitest frontend; backend pytest runs with `--import-mode=importlib`. Tests mirror source: backend `tests/<package>/<module>/test_<file>.py`, frontend `webui/src/<module>/__tests__/`. Rendered-component tests may use jsdom via Vitest when helper-level assertions are not enough. Pattern AAA; independent, deterministic, no shared state.

**Text assertions:** Assert a concrete string only when the text itself is a stable contract (protocol token, persisted format, accessibility name, forbidden internal value) or a test-owned sentinel proves transport unchanged. Do not lock editable prose, error wording, or help copy - prefer exception types, error codes, structured fields, DOM roles, and security invariants. Wording quality belongs in scenario evals, not substring tests.

**Quality gates:** `quality.py` (backend) and `quality-frontend.py` (frontend), same interface: format -> lint -> type-check -> test over the given paths, whole repo with none; a full run uses the default auto-fix mode (keep every fix), while scoped Agent runs must pass `--check` and therefore validate only. This preserves the code an Agent just read while it responds to reported failures. These gates are the contract - do not invoke pytest/ruff/vitest by hand; if you suspect a gate withheld something you need, note it in FLAGGED.md instead of making hand-invocation a habit. Full mechanics - pipeline, source-to-test mapping, output contract - live in `scripts/README-quality.md`.
```bash
python scripts/quality.py                                 # Full backend gate; auto-fixes
python scripts/quality.py --check <paths...>              # Scoped backend gate; no source edits
python scripts/quality-frontend.py                        # Full frontend gate; auto-fixes
python scripts/quality-frontend.py --check <paths...>     # Scoped frontend gate; no source edits
```

## Live Testing

Before live testing the WebUI in a browser, read `.vorch/workflows/web-test-workflow.md` in full. Before live testing the CLI, read `.vorch/workflows/cli-test-workflow.md` in full. Read both when the task spans both accessors.

## End-to-End Testing

The Playwright suite lives under `tests/e2e/` and is excluded from both quality gates. Release CI runs it as a required pre-publish gate via `.github/workflows/e2e.yml`. Run it locally only on explicit user request, and read `.vorch/workflows/e2e-test-workflow.md` in full before every run.

## Context

Use this section only for strategic decisions or global constraints an agent would otherwise assume incorrectly.

- **Deployment target is Linux, development happens on Windows.** The server runs headless on a Raspberry Pi (64-bit OS); desktop/CLI accessors stay on Windows. Keep core/server/cli platform-neutral: no Windows-only assumptions without a POSIX branch, process management branches on `os.name`/`sys.platform`, path validation accepts/rejects both path flavors on any host.
- **Kernel-to-Model notifications have a fixed set of sanctioned channels** (persisted notes rendered as system reminders, System Prompt blocks, Tool definitions/results). Whatever the domain - Extension, Channel, Tool, automation - pick an existing channel per `model-communication.md`; never invent a new one.
