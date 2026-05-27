# Project Context

## Project

vBot is a local-first agent harness — a runtime that gives agents maximum agency with minimal restrictions. A single async Python kernel powers four accessors: a FastAPI server, a Svelte web UI, a pywebview desktop shell, and a CLI.

Agents are first-class citizens with tool access to the host system. They can read and edit the application source (self-healing — fixing bugs they encounter during their work, or adding small features on the fly), configure the system via the CLI (set up Telegram channels, add API providers, switch the agent's model, etc.), and trigger application restarts to apply changes. The agent lives where the server lives; desktop and CLI are accessors.

This is a technical-user tool. The agent has the same capabilities as the user, with a small set of critical guardrails.

## Architecture

**Tech stack:** Python 3.11+ (hatchling), FastAPI + WebSocket + SSE, Svelte (JS,
no TypeScript), pywebview. Async-first — asyncio throughout the kernel, threads
only where native libraries force them.

**Layers:**
```
core/          ← Kernel (async). No HTTP, no UI.
server/        ← FastAPI + WS + SSE. Imports core/. RPC delegates per domain.
webui/         ← Svelte frontend. Own package.json. Talks HTTP/WS/SSE only.
cli/           ← CLI accessor. Server lifecycle locally; all other domains via RPC.
desktop/       ← pywebview shell. Imports nothing from the project — HTTP only.
```

**Core modules:** runtime, models, chat, runs, compaction, sessions, settings, prompts, attachments, extensions, agents, subagents, tools, providers, channels,
speech, skills, automation, storage, utils. Each is a folder with a main file as
public API, soft limit 600 lines per file. Providers has a subfolder structure:
`providers/` contains the adapter ABC, generic OpenAI-compatible and Anthropic
adapters, OpenAI-compatible provider-specific subclasses for provider deviations,
GitHub Copilot endpoint helpers and runtime policy, shared HTTP utilities, and
error classes in addition to the registry.
Automation now includes both `TriggerService` for queued programmatic run starts
and `CronService` for persisted time-based scheduling rooted at `<data_dir>/cron/`.

**Communication:** `POST /api/rpc` (method dispatcher) + `/ws` (event-bus push)
+ `/ws/logs` (selected log-file live tail) + SSE (streaming) + dedicated attachment HTTP endpoints (`POST /api/upload`, `GET /api/attachments/{id}`). No auth
(single-user-local).

**Data flow:** Accessors → HTTP/WS/SSE → server delegates → core (orchestration
via providers, models, tools, agents) → external APIs. Agentic-only — no
separate non-agentic streaming path.

**Configuration:** `settings.json` for application settings, `.env` for API keys
and bot tokens (belongs to the user, read at startup as fallback credential
source). Both live in the data directory (`~/.vbot`). Process environment keeps
higher precedence than the data-dir `.env`; vBot does not rewrite `os.environ`
from `.env` values. `settings.json` may include `skill_directories`, an array of
absolute or home-relative additional skill scan roots configured from the
Settings UI. Saving skill directories through `settings.update` reloads the
runtime skill registry immediately. `settings.json` may also include
`extension_directories`, an array of absolute or home-relative additional
extension scan roots loaded alongside `<data_dir>/extensions/` during runtime
startup, plus `attachment_max_size_bytes`, an integer attachment upload limit
used by the runtime-owned `AttachmentStore` (default 20 MiB). `settings.json`
also supports `defaults.agent` for project-wide fallback agent values
(`model`, `fallback_model`, `temperature`, `thinking_effort`); agents with
unset values inherit these defaults at load time without rewriting their
`agent.json` on disk. Optional built-in tool credentials such as
`BRAVE_API_KEY` for `web_search` use that same process env → data-dir `.env`
fallback resolution.

**I18n:** Every user-visible string through the i18n system from day 1. English
fallback. Backend: `utils/`, Frontend: `webui/src/lib/i18n.js`.

## Conventions

**Deep modules — few, large, simple interface:** We want few deep modules, not
many shallow ones. A deep module hides a lot of functionality behind a simple
interface. 

**Dependency injection:** Constructor injection via `__init__`. Interfaces via
`typing.Protocol`. No service locator, no global singletons, no `getattr` tricks.

**Error handling:** Base classes in `core/utils/errors.py`, domain-specific
extensions per module. Expected errors → handle locally, log `warn`. Unexpected
errors → rethrow, log `error`. Transient HTTP errors → max 3 retries, exponential
backoff + jitter. Provider errors classified as `retryable` vs `fatal`. No silent
`except Exception: pass`.

**Logging:** Structured logging via `LogManager` from `core/utils/logging`.
All application logs go through that pipeline and use per-module
`vbot.<domain>` loggers. Required format: `timestamp [LEVEL] name - message`.
Logs live under `<data_dir>/logs/`; `LogManager` handles the file layout. No
`print()`, no `logging.basicConfig()`, and no ad-hoc formatting. Routine `/ws`
and `/ws/logs` websocket lifecycle noise (`connection open`, `connection
closed`, and accepted-handshake lines) is filtered out of normal INFO logs;
transport errors must still remain visible. The Logs viewer also filters that
same routine websocket noise at read/stream time so older matching rows
already on disk do not remain visible in the Logs tab.

**Naming:** Descriptive, no abbreviations (except `id`, `url`, `db`). One thing
per function, max 3 nesting levels.

**Imports:** stdlib → third-party → local. Blank line between groups. Remove
unused.

**Time:** Persisted timestamps in UTC with explicit offset (ISO 8601). UI renders
in user timezone. No implicit `datetime.now()`.

**No legacy compatibility in app code — ever.** We are in development; schemas
and config formats can and will break. The app reads the current format and nothing
else. No auto-migrations, no fallback keys, no "if old_field then…" branches in
application code. If a format changes, the old version is simply invalid. Manual
conversion scripts go in `scripts/converters/` — they are standalone tools run
explicitly by the user, not hooked into app startup or storage layers.

**Frontend:** Svelte with JavaScript (no TypeScript). All user-visible strings
through i18n — no hardcoded text.

## Development

**Prerequisites:** Python >= 3.11, Node.js (for webui).

**Setup:**
```bash
pip install -e ".[dev]"
```

Use the current Python interpreter directly. Do not assume a virtual
environment for installs, quality gates, or runtime commands.

**Worktree commands:** Project worktrees are managed with:
```bash
python scripts/worktree.py create <task-name>
python scripts/worktree.py list
python scripts/worktree.py delete <task-name> [--force]
```
`create` prints the worktree `path`, assigned `port`, data dir, and URL.
`delete --force` discards uncommitted worktree changes. 
If worktree commands fail or behave unexpectedly, read `scripts/README-worktree.md`.

**Dependency groups:** `server`, `cli`, `desktop`, `dev`. Core dependencies: `httpx`, `pyyaml` (direct `SKILL.md` YAML frontmatter parsing), `croniter` (cron expression parsing / next-fire calculation), `tzdata` (cross-platform IANA timezone data for cron scheduling), and `beautifulsoup4` (HTML parsing for the built-in `web_fetch` text-extraction tool). The `server` group includes `watchfiles` for the dedicated log-view watcher transport, `python-telegram-bot` for channel adapters, and `python-multipart` for FastAPI multipart upload parsing. The `cli` group includes `psutil` for safe local process lookup during server lifecycle management. The `dev` group includes server transport dependencies, multipart upload parsing, the log-view watcher dependency, Telegram channel adapter dependencies, and CLI process-management dependencies so backend quality gates exercise FastAPI/SSE/WebSocket, channel flows, upload endpoints, and CLI tests. The WebUI runtime dependencies currently include `markdown-it` for assistant-message Markdown rendering in the chat timeline. See `pyproject.toml` and `webui/package.json` for exact packages.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```

**Build frontend:**
```bash
cd webui && npm install && npm run build   # Svelte → static JS/CSS
```

**Data directory:** `~/.vbot` — created on first run. Contains `.env` (API
keys), `settings.json`, `attachments/` blobs plus per-blob sidecar JSON,
`logs/`, OAuth tokens under `oauth/`, scheduled cron jobs under `cron/jobs.json`,
and all runtime data.

## Testing

**Framework:** pytest (backend), Vitest (frontend). Backend pytest uses
`--import-mode=importlib` so mirrored test modules may share basenames without
collection collisions. Frontend rendered-component tests may use `jsdom` via
Vitest when helper-level assertions are not enough.

**Structure:** Tests mirror source. Backend: `tests/<package>/<module>/test_<file>.py`.
Frontend: `webui/src/<module>/__tests__/` mirroring source (e.g. `src/lib/__tests__/` for library tests, `src/components/__tests__/` for component tests).

**Pattern:** AAA. Independent, deterministic, no shared state.

**Quality gates:** Two scripts with the same interface — each runs format → lint
→ type-check → test (→ build for frontend). Both accept one or more paths (files
or directories), or no args for full scan.
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

Live testing means starting the running app and verifying behavior via CLI,
API, and browser — not writing unit or integration tests (that is the Builder's
job with pytest/Vitest). The Tester agent owns live testing.

All project-specific live testing instructions — startup, health check,
browser strategy, which features need API credentials, shutdown — live in
**`.vorch/TESTER.md`**. The Tester agent reads this file on every session.

## Context

Use this section only for important strategic decisions, unusual global
constraints, or things an agent would otherwise likely assume incorrectly.

- **CLI is an accessor, not a second control plane.** Only `server start`,
  `server stop`, `server restart`, and `server status` act locally. Every other
  CLI area must require a reachable vBot server and go through the server RPC
  contract rather than reading or mutating files directly.
- **Agent CLI management is RPC-backed.** `agent list/show/create/update/delete`
  commands are an accessor over public Agent RPCs. They may change mutable Agent
  config fields, allowlists, and current session pointers, but must not mutate
  workspaces directly or bypass server-side last-agent/reference/busy checks.
- **Management CLI areas stay thin.** Channel update, tool catalog, prompt
  fragment, and log inspection commands are RPC accessors over the server
  contract. Prompt updates may read a local source file for input text, but the
  server remains responsible for validation and persistence; log commands read
  through `log.*` RPCs rather than direct data-dir file access.

- **Raw model catalog files are kept alongside sanitized files.** After a
  model-db refresh, `resources/models/<provider>.raw.json` stores the full
  provider HTTP response body for inspection and debugging. The app does not
  load or use raw catalog files at runtime. Only
  `resources/models/<provider>.json` (the sanitized vBot-schema catalog) is
  loaded by `ModelRegistry`. The raw file exists so we can see what providers
  actually returned and what additional fields might be useful to normalize
  later.
- **Discoverable provider facts belong in adapters, not in generated model files.**
  Provider catalogs under `resources/models/` are refreshable artifacts and may
  be regenerated by model-db updates. If a fact can be learned from a
  provider's models endpoint, other provider API responses, or a probe request
  against the real inference endpoint, the durable fix belongs in adapter
  catalog normalization or adapter runtime behavior rather than in hand-edited
  generated files.
- **Overrides are for research-only gaps.**
  `resources/models/<provider>.overrides.json` is reserved for durable facts the
  provider APIs do not expose and that we only know because we verified them
  externally. Do not use overrides for information the adapter can discover,
  infer, or validate from provider responses.
- **Two-channel transport architecture:** SSE is the per-Run streaming channel
  (token-by-token output for one Run). WebSocket is the persistent app-wide
  signalling channel (connection status, agent CRUD, run lifecycle summaries).
  SSE and WS serve different purposes and should not be merged.
- **WebSocket is server-push only.** Clients send requests via `POST /api/rpc`.
  The WS channel broadcasts server events; it does not accept client commands.
- **GitHub Copilot exact-model quirks belong in runtime policy, not in catalog overrides.**
  For GitHub Copilot specifically, bundled metadata tests and some lookup paths
  read `resources/models/github-copilot.json` directly and therefore bypass
  `resources/models/github-copilot.overrides.json`; exact invariants that must
  survive refresh belong in `core/providers/github_copilot_policy.py` via
  exact-model overrides.
- **System reminders are kernel-internal notes.** Chat sessions may persist `role: "note"` entries for background events. The chat loop embeds them into provider requests as synthetic user messages wrapped in `<system-reminder>` tags; provider adapters must never receive `role: "note"`, and the normal UI should not present notes as user messages.
- **Built-in slash commands are a pre-run command layer, not skill triggers.** Recognized pure-text commands such as `/stop` are intercepted by a shared `CommandDispatcher` in server and channel entry points before any Run starts. Unknown slash text still goes through the normal chat path, so existing `/skill-name` activation behavior remains unchanged.
- **User-explicit skill triggers are skill-only activation hints.** `/skill-name` at the start of a message and `$skill-name` anywhere in a message activate allowed loadable skills before the provider request while preserving the original user message verbatim. `/` autocomplete may show built-in commands plus skills because slash is the shared command/skill entry point; `$` autocomplete must show skills only and must never surface built-in slash commands.
- **Skill availability is machine-checkable through vBot metadata.** Skills may define `metadata.vbot.requirements` with nested `all`/`any`/`optional` checks over `env`, `binary`, and `skill`. `compatibility` is not interpreted. Prompt catalogs, autocomplete, and skill activation use only currently available skills; `skill.list` still reports unavailable skills and missing requirements.
- **Busy-session queueing is owned by `ChatRunManager`.** Browser sends, `TriggerService`, and subagent routing all enqueue into the same in-memory FIFO per `(agent_id, session_id)` inside `core/chat/runs.py`. WebUI queue state is only a server-backed projection and must not become a second source of truth.
- **Agent model strings may carry pinned connection suffixes.** Persisted `Agent.model` and `Agent.fallback_model` may end with `::<connection-local-id>` to pin a provider-local connection without separate `connection` fields. Runtime reconstructs `<provider>:<connection-local-id>` from the model prefix, and code that needs the plain catalog key must strip the suffix first.
- **Agent defaults resolve at read time, not write time.** `settings.json`
  owns `defaults.agent`, while persisted `agent.json` keeps only explicit
  per-agent overrides. Empty model strings and `null` temperature or
  `thinking_effort` values mean "inherit the current default"; `AgentStore`
  applies defaults fresh on each `get()` and `list()` so changing settings
  immediately affects unresolved agents without baking values back to disk.
- **Adapters get provider-scoped catalog lookup from runtime.** If adapter runtime behavior depends on normalized model facts, use the injected read-only `model_lookup(model_id) -> Model | None` inside the adapter layer. Do not add provider-specific runtime helper methods for catalog access, and do not load model files directly from adapters.
- **Extensions are in-process Python hook modules.** Runtime loads them from
  `<data_dir>/extensions/` plus optional `extension_directories`, exposes them
  as `runtime.extensions`, and treats hook execution as fail-open: extension
  load/register failures log at error, handler failures log at warn, and normal
  runs continue.
- **Channel architecture decisions are fixed by `stuff/channels.md`.** Decisions D1-D8 are the binding contract for the first channel implementation: automatic final-reply routing via `run.subscribe()`, session metadata sidecars owned by `ChatSessionManager`, `channel_send` as proactive outbound only, adapter-local batching/sequencing, per-channel runtime start/stop, and deny-all default `allowed_chat_ids`.

## Specs

Domain-specific documentation lives in `.vorch/specs/`. A **domain** is any module or subsystem that has its own folder or clear boundary in the codebase — a chunk of code that has a distinct responsibility and that agents need context about before touching it. This includes technical modules (`hooks`, `tools`, `storage`), infrastructure modules (`server`, `channel`), and business modules (`auth`, `payments`). Size doesn't matter — what matters is that working on it without context risks misunderstanding its interfaces or conventions.

**When working on a domain: read its spec file.** Your task will list which specs are relevant — treat that as a starting point, not a ceiling. Read additional specs if you need them.

| Spec file | Domain | What it covers |
|---|---|---|
| `.vorch/specs/runtime.md` | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| `.vorch/specs/providers.md` | `core/providers/` | Provider domain overview and index to provider-specific specs |
| `.vorch/specs/models.md` | `core/models/` | Model data classes, registry, capabilities, model ID convention |
| `.vorch/specs/chat.md` | `core/chat/` | Canonical ChatMessage format, chat-loop constraints, Run execution |
| `.vorch/specs/runs.md` | `core/runs/` | Run lifecycle, cancellation, timeline events, in-memory queues |
| `.vorch/specs/compaction.md` | `core/compaction/` | Context-window compaction, checkpoints, summary strategy |
| `.vorch/specs/sessions.md` | `core/sessions/` | Session persistence, metadata, current JSONL storage contract |
| `.vorch/specs/settings.md` | `core/settings/` | Public settings update schemas, validation, parser errors |
| `.vorch/specs/prompts.md` | `core/prompts/` | System Prompt assembly, editable fragments, prompt variables |
| `.vorch/specs/attachments.md` | `core/attachments/` | Blob storage, MIME sniffing, attachment metadata, text extraction |
| `.vorch/specs/extensions.md` | `core/extensions/` | Extension hook loading, handler registration, runtime/chat event contracts |
| `.vorch/specs/agent.md` | `core/agents/` | Agent schema, persistence, workspace lifecycle, archive-on-delete |
| `.vorch/specs/subagents.md` | `core/subagents/` | Sub-agent coordinator, in-memory batch tracking, parent-child run linkage |
| `.vorch/specs/tools.md` | `core/tools/` | Tool domain overview and index to tool-specific specs |
| `.vorch/specs/storage.md` | `core/storage/` | Data-directory setup, settings persistence, prompt fragments |
| `.vorch/specs/skills.md` | `core/skills/` | Local skill metadata loading and prompt allowlist filtering |
| `.vorch/specs/automation.md` | `core/automation/` | Programmatic run triggering and in-memory queue semantics |
| `.vorch/specs/channels.md` | `core/channels/` | Channel configs, adapter lifecycle, Telegram-first routing, metadata, outbound send |
| `.vorch/specs/server.md` | `server/` | RPC envelope, FastAPI app, SSE/WebSocket transport, static WebUI serving |
| `.vorch/specs/cli.md` | `cli/` | Local server lifecycle commands, targeting rules, status/logging contract |
| `.vorch/specs/desktop.md` | `desktop/` | pywebview thin-client contract, target URL, window lifecycle, local settings |
| `.vorch/specs/webui.md` | `webui/` | Svelte app shell, API client, Chat/Agents views, queue behavior |
| `.vorch/specs/logs.md` | log viewer subsystem | Daily log parsing, log RPC/socket contract, WebUI Logs tab behavior |
