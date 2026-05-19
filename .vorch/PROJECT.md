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
cli/           ← Server management. Imports core/. Used by both users and agents.
desktop/       ← pywebview shell. Imports nothing from the project — HTTP only.
```

**Core modules:** runtime, models, chat, attachments, extensions, agents, tools, providers, channels,
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
used by the runtime-owned `AttachmentStore` (default 20 MiB).

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

**Dependency groups:** `server`, `cli`, `desktop`, `dev`. Core dependencies: `httpx`, `pyyaml` (direct `SKILL.md` YAML frontmatter parsing), `croniter` (cron expression parsing / next-fire calculation), and `tzdata` (cross-platform IANA timezone data for cron scheduling). The `server` group includes `watchfiles` for the dedicated log-view watcher transport, `python-telegram-bot` for channel adapters, and `python-multipart` for FastAPI multipart upload parsing. The `cli` group includes `psutil` for safe local process lookup during server lifecycle management. The `dev` group includes server transport dependencies, multipart upload parsing, the log-view watcher dependency, Telegram channel adapter dependencies, and CLI process-management dependencies so backend quality gates exercise FastAPI/SSE/WebSocket, channel flows, upload endpoints, and CLI tests. The WebUI runtime dependencies currently include `markdown-it` for assistant-message Markdown rendering in the chat timeline. See `pyproject.toml` and `webui/package.json` for exact packages.

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

- **Raw model catalog files are kept alongside sanitized files.** After a
  model-db refresh, `resources/models/<provider>.raw.json` stores the full
  provider HTTP response body for inspection and debugging. The app does not
  load or use raw catalog files at runtime. Only
  `resources/models/<provider>.json` (the sanitized vBot-schema catalog) is
  loaded by `ModelRegistry`. The raw file exists so we can see what providers
  actually returned and what additional fields might be useful to normalize
  later.
- **Two-channel transport architecture:** SSE is the per-Run streaming channel
  (token-by-token output for one Run). WebSocket is the persistent app-wide
  signalling channel (connection status, agent CRUD, run lifecycle summaries).
  SSE and WS serve different purposes and should not be merged.
- **WebSocket is server-push only.** Clients send requests via `POST /api/rpc`.
  The WS channel broadcasts server events; it does not accept client commands.
- **Do not hand-edit provider-generated model catalogs for durable Copilot fixes.**
  `resources/models/<provider>.json` is refreshable provider data and may be
  overwritten by model-db updates. Durable Copilot metadata corrections belong
  in the runtime policy layer. For GitHub Copilot specifically, bundled
  metadata tests and some lookup paths read `resources/models/github-copilot.json`
  directly and therefore bypass `resources/models/github-copilot.overrides.json`;
  exact invariants that must survive refresh belong in
  `core/providers/github_copilot_policy.py` via exact-model overrides.
- **System reminders are kernel-internal notes.** Chat sessions may persist `role: "note"` entries for background events. The chat loop embeds them into provider requests as synthetic user messages wrapped in `<system-reminder>` tags; provider adapters must never receive `role: "note"`, and the normal UI should not present notes as user messages.
- **Built-in slash commands are a pre-run command layer, not skill triggers.** Recognized pure-text commands such as `/stop` are intercepted by a shared `CommandDispatcher` in server and channel entry points before any Run starts. Unknown slash text still goes through the normal chat path, so existing `/skill-name` activation behavior remains unchanged.
- **Agent model strings may carry pinned connection suffixes.** Persisted `Agent.model` and `Agent.fallback_model` may end with `::<connection-local-id>` to pin a provider-local connection without separate `connection` fields. Runtime reconstructs `<provider>:<connection-local-id>` from the model prefix, and code that needs the plain catalog key must strip the suffix first.
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
| `.vorch/specs/providers.md` | `core/providers/` | Provider config, adapter hierarchy, wire protocols, error classification |
| `.vorch/specs/models.md` | `core/models/` | Model data classes, registry, capabilities, model ID convention |
| `.vorch/specs/chat.md` | `core/chat/` | Canonical ChatMessage format, JSONL sessions, chat-loop constraints |
| `.vorch/specs/attachments.md` | `core/attachments/` | Blob storage, MIME sniffing, attachment metadata, text extraction |
| `.vorch/specs/extensions.md` | `core/extensions/` | Extension hook loading, handler registration, runtime/chat event contracts |
| `.vorch/specs/agent.md` | `core/agents/` | Agent schema, persistence, workspace lifecycle, archive-on-delete |
| `.vorch/specs/tools.md` | `core/tools/` | Tool metadata, allowlist filtering, provider definitions, dispatch |
| `.vorch/specs/storage.md` | `core/storage/` | Data-directory setup, settings persistence, prompt fragments |
| `.vorch/specs/skills.md` | `core/skills/` | Local skill metadata loading and prompt allowlist filtering |
| `.vorch/specs/automation.md` | `core/automation/` | Programmatic run triggering and in-memory queue semantics |
| `.vorch/specs/channels.md` | `core/channels/` | Channel configs, adapter lifecycle, Telegram-first routing, metadata, outbound send |
| `.vorch/specs/server.md` | `server/` | RPC envelope, FastAPI app, SSE/WebSocket transport, static WebUI serving |
| `.vorch/specs/cli.md` | `cli/` | Local server lifecycle commands, targeting rules, status/logging contract |
| `.vorch/specs/desktop.md` | `desktop/` | pywebview thin-client contract, target URL, window lifecycle, local settings |
| `.vorch/specs/webui.md` | `webui/` | Svelte app shell, API client, Chat/Agents views, queue behavior |
| `.vorch/specs/logs.md` | log viewer subsystem | Daily log parsing, log RPC/socket contract, WebUI Logs tab behavior |
