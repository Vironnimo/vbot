# Project Context

## Project

vBot is a self-hosted environment for working with AI Agents across conversations, Projects, and automated tasks. It brings together persistent Sessions, Agent identity and Memory, project context, and Tools for acting on the host system and connected services. Agents can work directly with the user or carry out tasks independently.

The runtime and shared state live on the server. WebUI, Desktop, CLI, and Channels provide different ways to interact with the same system.

## Architecture

**Stack:** Python 3.11+ (hatchling), FastAPI + WebSocket + SSE, Svelte (JS, no TypeScript), pywebview. Kernel uses asyncio; threads only where native libraries require them.

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

The optional voice companion uses direct browser-to-OpenAI WebRTC media, initialized by server RPC with the existing OpenAI API-key Connection. App actions use existing Chat/Terminal operations; see `model_tasks/live.md` for details and secure-context limits.

**Persistence:** Canonical Session history: normalized columns in `<data-dir>/sessions.db` (SQLite `STRICT`, WAL where safe, `synchronous=FULL`; `session-store.json` authorizes creation). External-content FTS indexes searchable Messages; a second trigram index excludes Tool-role bulk. No mirrored search-text table. Verified `session-snapshots/` under the data directory provide auto-restore; only explicit operator, update, or converter workflows create them. Normal Runtime startup/operation never copies the database. `session-recovery.json` records incidents.

**Tools:** Schemas, argument normalization/validation, concurrency: `tools.md`. Agent-facing definition design: `tools/designing-agent-tools.md`.

**Extensions:** API 6 provides owner-bound temporary Sessions/execution groups and isolated built pages. Canonical Sessions own binding, receipt and Run identity; Extension databases own domain state. The app provides a generic page bridge; Extensions own domain UI (`extensions.md`).

**Configuration:** Data directory `~/.vbot` owns `settings.json` and `.env`, a user-owned fallback credential snapshot. Process environment takes precedence; vBot never rewrites `os.environ`. Owning domains validate every user-editable JSON file before runtime use; public accessors configure Settings only through cataloged paths. See `settings.md`, `storage.md`, and `providers/connections.md` (Custom Provider credentials).

## Domain Maps

Read domain roots and task-relevant references under `.vorch/domain-maps/` as described in `AGENTS.md` -> Load context for the task. Maps orient you to owners, boundaries, contracts, source, and tests. Code establishes implemented behavior; user requirements and engineering contracts establish obligations. Correct stale descriptions without treating existing behavior as the desired outcome (see `AGENTS.md` -> Interpret documentation).

| Map | Domain | Covers |
|---|---|---|
| runtime.md | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| providers.md | `core/providers/` | Provider boundary, Connection/discovery/request/usage invariants |
| models.md | `core/models/` | Model DB layers, registry, capabilities, id convention |
| model_tasks.md | `core/model_tasks/` | Task-model bindings, target discovery, option schemas |
| chat.md | `core/chat/` | ChatMessage boundary, Agentic Loop invariants |
| runs.md | `core/runs/` | Run lifecycle, cancellation, timeline events, queues |
| compaction.md | `core/compaction/` | Triggers, strategies, plans, checkpoints |
| sessions.md | `core/sessions/` | Canonical SQLite Session persistence, metadata, offline conversion boundary, and lifecycle |
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

## Conventions

**Dependency injection:** Constructors (`__init__`) and `typing.Protocol` interfaces; no service locator or global singletons.

**Object IDs:** Use `core/utils/ids.py` for vBot-owned references: short type prefix + 12 lowercase base32 characters (60 random bits) with atomic owner-side uniqueness claims; 16 characters (80 bits) for pre-persistence/high-volume identities lacking a complete allocation catalog (Messages, Runs, Queue items). Storage and Tool results use the same id; no display aliases or abbreviated incoming ids. Preserve existing opaque ids exactly. Path-backed readers validate safe basenames, not generator format. Owners authorize access; prefixes/randomness do not. Provider/protocol ids, credentials, hashes, and private storage generations keep their own contracts. Tests: `tests/core/utils/test_ids.py` and owner collision/round-trip tests.

**Errors:** Base classes in `core/utils/errors.py`, domain subclasses per module. Expected errors: handle locally, log `warn`; unexpected: rethrow, log `error`. Never silently swallow. Transient HTTP retries use shared `core/utils/retry.py` backoff and idempotency-aware statuses in `core/utils/http_status.py`. Provider errors are `retryable` or `fatal`.

**Logging:** Structured logs via `LogManager` (`core/utils/logging`), per-module `vbot.<domain>` loggers, `<data_dir>/logs/`. Standalone Desktop uses the same format without importing core logging. No `print()` or `logging.basicConfig()`. Each material control-plane mutation emits one post-change `INFO` event: operation, stable target ids, changed fields. Never log credentials, token values, Provider Account ids, Prompt/Skill/Cron content, or external conversation ids. Reads, polls, appearance changes, acknowledgements, routine traffic, and effective no-ops stay silent. Operational failures and health transitions use `WARNING`/`ERROR`.

**Time:** Persist ISO 8601 UTC timestamps with explicit offset. Optional IANA `timezone` defaults to the server host zone; once Settings resolve, it alone controls Agent context, wall-clock Calendar/Cron behavior, and UI rendering, never implicit browser/host time. No implicit `datetime.now()`.

**No legacy compatibility in app code.** Development schemas/config formats may break; only the current format is valid. No auto-migrations, fallback keys, or old-field branches. Manual converters belong in `scripts/converters/`, run explicitly by the user, never from startup or storage layers.

**I18n:** All user-visible strings use i18n with English fallback: backend `utils/`, frontend `webui/src/lib/i18n.js`.

**Model-facing paths:** Render separators as `/` when vBot authors a known filesystem-path value for Model context (System Prompt, attachment note, Tool result, delivery note). Leave `pathlib.Path`, native OS calls, persisted values, incoming arguments, and arbitrary text unchanged; no global replacement.

## Development

**Setup:** Python >= 3.11, Node.js for WebUI; editable install with dev extras:
```bash
pip install -e ".[dev]"
```
Use the current interpreter; do not assume a virtual environment for installs, gates, or runtime commands. Before editing installer/uninstall scripts in `scripts/`, read [USAGE.md](../USAGE.md#installation) for end-user installation/update/removal.

**Worktrees:** `python scripts/worktree.py create|list|merge|delete <task-name>`; also `repair-start|repair-finish`. `create` reports path, ports, data dir, URL. Non-force `delete` fails closed on Git removal errors unless `git worktree list` confirms deregistration; `delete --force` discards uncommitted work. `merge` lands the task branch on `main` and removes the worktree, with a merge lock and protected conflict-repair window. The agent must pass quality gates before merging; this tool never runs them. On failure/unexpected behavior, read `scripts/README-worktree.md`.

**Dependencies:** `pyproject.toml` groups: `server`, `cli`, `desktop`, `local-speech`, `local-tts`, `dev`; frontend: `webui/package.json`. Optional server-native Transformers/PyTorch STT supports Qwen3 ASR, Parakeet, Nemotron 3.5 ASR. `core/model_tasks/speech_setup.py` owns in-app setup; Settings installs the shipped extra into the running server's interpreter. Core dependency `psutil` provides verified process-tree cleanup and server restart support. Local TTS uses isolated managed SDK environments for Qwen3-TTS and Chatterbox Multilingual V3, with standalone child entry point `speech_worker.py`. See `USAGE.md` -> Local speech recognition / synthesis.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```
A git-ignored checkout marker selects dev data `~/.vbot-dev`, port `8421`. Installed CLI outside the checkout uses product defaults `~/.vbot`, `8420`. Never target the installed instance with development commands. Managed worktrees have separate data dirs and ports.

**Session store:** Live operator-safe health: `python cli/main.py session-store status|snapshot|incident`; `snapshot restore` requires a proven-stopped target. `python scripts/converters/session_sqlite.py inventory|dry-run|convert|verify|install|resume|export-jsonl` is only for explicit offline work on copied legacy data.

**Frontend build:** `cd webui && npm ci && npm run build`. Also compiles bundled Extension `ui/page.html` entries to relative `web/` assets via `webui/scripts/build-extension-pages.mjs`; installers ship assets and Extension sources. The frontend gate covers these external source/test paths with the shared dependency tree.

**Release:** Read `.vorch/workflows/release-workflow.md` when the user requests a release.

## Testing

Backend: pytest with `--import-mode=importlib`; frontend: Vitest, optionally jsdom when helper assertions cannot cover rendered components. Tests mirror source: `tests/<package>/<module>/test_<file>.py` and `webui/src/<module>/__tests__/`.

**Text assertions:** Exact strings only for stable contracts (protocol tokens, persisted formats, accessibility names, forbidden internal values) or test-owned transport sentinels. For editable prose, errors, and help, assert exception types, codes, structured fields, DOM roles, or security invariants instead. Evaluate wording in scenarios, not substring tests.

**Quality gates:** `scripts/quality.py` (backend) and `scripts/quality-frontend.py` share format -> lint -> type-check -> test, scoped by paths or full with none. Use `--check` for non-mutating development/CI feedback; before commits use scoped auto-fix mode and keep every fix, so tests cover the fixed code. Include affected callers/tests explicitly; mapping misses cross-domain dependencies. Use full gates for broad or unscopable effects. Frontend pre-commit requires `--build`: whole WebUI build, unchanged lint/test scope. Use gates, not direct pytest/ruff/vitest; record suspected gate omissions in `.vorch/FLAGGED.md`. Pipeline, test mapping, and output details: `scripts/README-quality.md`.
```bash
python scripts/quality.py <paths...>                  # Backend pre-commit
python scripts/quality.py --check <paths...>          # Backend feedback
python scripts/quality.py --check --profile           # Full check + 25 slowest tests
python scripts/quality-frontend.py --build <paths...>  # Frontend pre-commit
python scripts/quality-frontend.py --check <paths...>  # Frontend feedback
# Omit paths for full gates on the affected side(s).
```

## Live Testing

Before live tests, fully read `.vorch/workflows/web-test-workflow.md` for browser WebUI testing, `.vorch/workflows/cli-test-workflow.md` for CLI testing; both for tasks spanning both accessors.

## End-to-End Testing

Playwright `tests/e2e/` is excluded from both quality gates; release CI requires it before publishing (`.github/workflows/e2e.yml`). Local runs require explicit user request and a full read of `.vorch/workflows/e2e-test-workflow.md` before every run.

## Context

Only strategic decisions or global constraints an Agent might otherwise misread belong here.

- **Linux deployment, Windows development:** Headless server on a 64-bit Raspberry Pi; Desktop/CLI on Windows. Keep core/server/cli platform-neutral: Windows-specific assumptions need POSIX branches; process management branches on `os.name`/`sys.platform`; path validation handles both path flavors on every host.
- **Kernel-to-Model notifications:** Only sanctioned channels from `model-communication.md`: persisted notes rendered as System Reminders, System Prompt blocks, Tool definitions/results. Every domain (including Extensions, Channels, Tools, automation) must use these; never invent a channel.
