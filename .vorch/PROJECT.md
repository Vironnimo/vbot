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

**Core modules:** runtime, models, chat, agents, tools, providers, channels,
speech, skills, automation, storage, utils. Each is a folder with a main file as
public API, soft limit 600 lines per file. Providers has a subfolder structure:
`providers/` contains the adapter ABC, OpenAI-compatible and Anthropic adapters,
shared HTTP utilities, and error classes in addition to the registry.

**Communication:** `POST /api/rpc` (method dispatcher) + `/ws` (event-bus push)
+ SSE (streaming). No auth (single-user-local).

**Data flow:** Accessors → HTTP/WS/SSE → server delegates → core (orchestration
via providers, models, tools, agents) → external APIs. Agentic-only — no
separate non-agentic streaming path.

**Configuration:** `settings.json` for application settings, `.env` for API keys
and bot tokens (belongs to the user, loaded at startup). Both live in the data
directory (`~/.vbot`).

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
Per-module loggers (`vbot.chat`, `vbot.tools`, …). Format: `timestamp [LEVEL]
name - message`. No `print()`, no `logging.basicConfig()`.

**Naming:** Descriptive, no abbreviations (except `id`, `url`, `db`). One thing
per function, max 3 nesting levels.

**Imports:** stdlib → third-party → local. Blank line between groups. Remove
unused.

**Time:** Persisted timestamps in UTC with explicit offset (ISO 8601). UI renders
in user timezone. No implicit `datetime.now()`.

**Frontend:** Svelte with JavaScript (no TypeScript). All user-visible strings
through i18n — no hardcoded text.

## Development

**Prerequisites:** Python >= 3.11, Node.js (for webui).

**Setup:**
```bash
python -m venv .venv
.\.venv\Scripts\activate   # Windows
pip install -e ".[dev]"
```

**Dependency groups:** `server`, `cli`, `desktop`, `dev`. Core dependency: `httpx`. The `cli` group includes `psutil` for safe local process lookup during server lifecycle management. The `dev` group includes server transport dependencies and CLI process-management dependencies so backend quality gates exercise FastAPI/SSE/WebSocket and CLI tests. See `pyproject.toml` for exact packages.

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

**Data directory:** `~/.vbot` — created on first run. Contains `.env` (API keys),
`settings.json`, and all runtime data.

## Testing

**Framework:** pytest (backend), Vitest (frontend).

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

## Context

**2026-05-01 — Phase 0 complete:** Scaffold with `pyproject.toml`, folder
structure, core utils (errors, logging, config), Runtime class with DI, and
smoke tests. `Runtime(Config()).start(); .stop()` runs without error. Core
modules not yet implemented.

**2026-05-02 — WebUI scaffold:** Minimal Vite + Svelte 5 + JS frontend in
`webui/`. `package.json` with devDependencies (svelte, vite, vitest, prettier,
eslint). Quality gate pipeline established: `python scripts/quality-frontend.py`
passes all five gates (prettier, eslint, vitest, build) on full and scoped scans.
Fixed `scripts/quality-frontend.py` to resolve `npx`/`npm` via `shutil.which()`
for Windows compatibility. No real frontend app yet — placeholder only.

**2026-05-03 — Phase 1 complete (Provider + Model System):** Two-layer
architecture implemented: Provider layer (wire protocol, auth, config) and
Model layer (provider-specific model data with registry). Adapter hierarchy
with `ProviderAdapter` ABC, `OpenAICompatibleAdapter` (covers OpenAI,
OpenRouter, Groq, Together), and `AnthropicAdapter` (own wire protocol).
Both registries load from JSON files in `resources/`, cache after first load,
and are wired into `Runtime.start()`. `Runtime.get_adapter()` factory resolves
API keys from environment and instantiates the correct adapter class. Error
hierarchy: `ProviderError` (retryable/fatal) with `ProviderAuthError`,
`ProviderRateLimitError`, `ProviderTimeoutError`. Retry utility with exponential
backoff + jitter. 166 tests passing. New core dependency: `httpx`. New dev
dependencies: `respx`, `pytest-asyncio`.

**2026-05-04 — Phase 2 domain foundations:** Added backend kernel primitives
for Phase 2 minimal chat: canonical chat messages and append-only JSONL
sessions, persisted agent CRUD with workspace seeding/archive-on-delete,
empty-by-default tool registry with allowlist filtering and async dispatch,
storage manager for data-dir/settings/prompt fragments, and local skill metadata
registry. Bundled workspace templates live in `resources/workspace-templates/`;
bundled prompt fragments live in `resources/prompts/`. No new dependencies.

**2026-05-04 — Phase 2 prompt/provider contracts:** Added `SystemPromptManager`
for assembling prompts from `resources/prompts/` fragments and workspace
includes, with filtered tool and skill metadata. Provider adapters now translate
canonical chat messages, provider tool definitions, `thinking_effort`, and
assistant response fields (`content`, `reasoning`, `reasoning_meta`,
`tool_calls`) at the adapter boundary so `core/chat/` stays provider-agnostic.
No new dependencies.

**2026-05-04 — Phase 2 runtime/chat orchestration:** Runtime now initializes
Phase 2 services (`StorageManager`, `AgentStore`, `ToolRegistry`,
`SkillRegistry`, `ChatSessionManager`, `SystemPromptManager`) alongside
providers/models and clears them on stop. Added `ChatLoop(runtime).send(...)`
for non-streaming backend turns: it persists the user message, assembles the
system prompt plus session history, calls the configured provider adapter,
dispatches allowed tool calls until final response or max iterations, and
persists assistant/tool messages in JSONL order. No new dependencies.

**2026-05-04 — Phase 2 integration hardening:** Added runtime-backed Phase 2
integration coverage for creating an agent, sending a non-streaming chat turn
through a fake provider adapter, persisting JSONL session messages, and verifying
prompt assembly with workspace includes plus filtered tool/skill metadata. Public
package exports were cleaned up for the new Phase 2 services. Full backend gate
passes with 298 tests. No new dependencies.

**2026-05-04 — Phase 2 review fixes:** Hardened session ID validation before
filesystem path construction, accepted UTC `Z` timestamps, closed chat adapters
after each non-streaming turn, grouped consecutive Anthropic tool results into a
single user message, preserved Anthropic thinking/redacted-thinking metadata as
opaque content blocks, and made malformed OpenAI tool-call argument JSON
normalize safely. No new dependencies.

**2026-05-04 — Phase 3 architecture clarified:** Server-side architecture is now
fixed before implementation: clients (WebUI, Desktop, CLI) talk to a stable
vBot server contract, while provider-specific transport remains hidden in
adapters. Sessions stay explicit and persisted as one JSONL history per
session; a Run is the active execution inside a session. At most one Run may be
active per session, but multiple sessions/agents may run in parallel. `send`,
`stream`, and `cancel` are the same execution model with different access
patterns. Client streaming is exposed via SSE, WebSocket is reserved for general
server events, and visible chat state should include thinking blocks, tool
steps/results, and assistant responses. No new dependencies.

**2026-05-04 — Phase 3 core run model:** `core/chat/` now has a provider-
agnostic Run abstraction and `ChatRunManager` for one active Run per Session,
parallel Runs across different Sessions, replayable/subscribable visible Run
events, and best-effort cancellation with late-output suppression. `ChatLoop.send()`
keeps Phase 2 compatibility while `ChatLoop.start_run()` requires an existing
Session for server-facing access modes. No new dependencies.

**2026-05-04 — Phase 3 server RPC foundation:** `server/` now exposes a FastAPI
app factory with runtime lifecycle wiring, health check, and `POST /api/rpc`
dispatcher. The initial delegates support explicit `session.create`,
`chat.send`, `chat.stream`, and `chat.cancel`; chat methods target existing
Sessions and return provider-agnostic Run envelopes without opaque provider
metadata. `server/main.py` starts uvicorn and resolves ports using `--port` >
`VBOT_SERVER_PORT` > `settings.json` > `8420`. No new dependencies.

**2026-05-04 — Phase 3 SSE/WebSocket transports:** `chat.stream` now returns an
SSE URL for the started Run, `GET /api/runs/{run_id}/events` replays and follows
the stable visible Run timeline until a terminal event, and `/ws` pushes general
run lifecycle summaries from an in-memory server event bus. SSE and WebSocket
payloads are provider-agnostic and strip opaque `reasoning_meta`. No new
dependencies.

**2026-05-04 — Phase 3 integration hardening:** Backend integration tests now
cover explicit Session creation, `chat.send`, `chat.stream` with SSE replay,
visible reasoning/tool/assistant events, JSONL persistence, same-Session active
Run rejection, parallel different-Session Runs, and best-effort cancellation
that suppresses late output and prevents further tool/model progression. Full
backend gate passes with 340 tests. No new dependencies.

**2026-05-04 — Phase 3 review fix:** The `dev` dependency group now includes
server transport dependencies (`fastapi`, `uvicorn[standard]`, `websockets`),
and server transport tests no longer skip silently when FastAPI is missing. This
ensures `python scripts/quality.py` actually verifies the HTTP, SSE, and
WebSocket Phase 3 exit criteria.

**2026-05-04 — Phase 4 behavior clarified:** Agents should persist an explicit
`current_session_id` in `agent.json`, and every new agent should get its first
Session immediately on creation. The first WebUI is planned around agent
selection rather than session selection, with no old-chat list in the UI.
Additional user messages during a running Run should queue FIFO for that
agent/current chat, remain visible in the UI, and be cancellable before send.
Remembering the last selected agent is desired later as accessor-local state
rather than shared server/data-dir state.

**2026-05-04 — Phase 4 WebUI minimal:** The server contract now exposes
WebUI-facing Agent RPCs (`agent.list`, `agent.create`, `agent.update`,
`agent.delete`), `chat.history`, and `session.create(make_current: true)`.
Runtime bootstrap creates `main` / `Main` with an initial Session when a data
directory has no agents, and Agent loading normalizes legacy configs to a valid
`current_session_id`. Public Agent RPCs validate mutable fields server-side;
`workspace` remains visible but is not editable through WebUI/RPC to avoid moving
arbitrary user paths on archive. Agent deletion is serialized inside one server
process to preserve the minimum-one-Agent invariant. FastAPI serves `webui/dist`
when present without shadowing `/api`, `/ws`, or `/health`. The Svelte app has a
two-pane shell, API client, Agent-first Chat view with SSE streaming/cancel and
an in-memory FIFO queue, Agent CRUD view, plus minimal System Prompt and Settings
placeholders. No new dependencies.

**2026-05-04 — Data-dir `.env` provider auth fix:** Runtime startup now loads
`<data_dir>/.env` into `os.environ` before provider adapters resolve API keys.
Existing process environment variables remain authoritative and are not
overwritten. The parser is conservative (`KEY=VALUE`, comments/blanks ignored,
matching quotes stripped) and does not log secret values. No new dependencies.

**2026-05-04 — Phase 5 CLI contract clarified:** The planned CLI server
management contract is now fixed before implementation. `server start` is
data-dir-scoped, waits for `/health`, prints the resolved URL, and never opens
the browser automatically. `server stop` / `server restart` / `server status`
may target any already-running local vBot server on the chosen address/port,
but must not stop non-vBot processes. Shutdown is best-effort graceful with a
timeout and force-stop fallback, which matters especially on Windows. Instance
logs belong under `<data_dir>/logs/`, `restart` re-resolves current
args/env/settings, and missing `webui/dist` should be reported as "server up,
WebUI unavailable" rather than treated as startup failure. vBot detection for
CLI lifecycle commands is based on the `/health` contract, and `server status`
should report running state, URL, WebUI availability, and resolved `data_dir`.

**2026-05-04 — Phase 5 CLI implemented:** `cli/main.py` now exposes
non-interactive `server start`, `server stop`, `server restart`, and
`server status` commands. `cli/server_management.py` resolves instances using
the shared server port priority, detects vBot targets through the exact
`/health` contract, redirects CLI-started server logs to `<data_dir>/logs/`,
reports WebUI availability separately from API health, and uses `psutil` only
after vBot identity is confirmed for graceful-then-forced local process stop.
Full backend gate passes with 471 tests. New CLI/dev dependency: `psutil`.

**2026-05-04 — Phase 5 CLI review hardening:** `server.main.resolve_port()` now
ignores ambient `PORT` and `SERVER_PORT` process environment variables; only
`VBOT_SERVER_PORT` can override `settings.json`. CLI process lookup now matches
the resolved host/address and port, with explicit wildcard listener handling, so
same-port listeners on other addresses are not selected for termination. Failed
startup attempts clean up the just-spawned child process before returning an
error. Full backend gate passes with 479 tests. No new dependencies.

## Specs

Domain-specific documentation lives in `.vorch/specs/`. A **domain** is any module or subsystem that has its own folder or clear boundary in the codebase — a chunk of code that has a distinct responsibility and that agents need context about before touching it. This includes technical modules (`hooks`, `tools`, `storage`), infrastructure modules (`server`, `channel`), and business modules (`auth`, `payments`). Size doesn't matter — what matters is that working on it without context risks misunderstanding its interfaces or conventions.

**When working on a domain: read its spec file.** Your task will list which specs are relevant — treat that as a starting point, not a ceiling. Read additional specs if you need them.

| Spec file | Domain | What it covers |
|---|---|---|
| `.vorch/specs/runtime.md` | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| `.vorch/specs/providers.md` | `core/providers/` | Provider config, adapter hierarchy, wire protocols, error classification |
| `.vorch/specs/models.md` | `core/models/` | Model data classes, registry, capabilities, model ID convention |
| `.vorch/specs/chat.md` | `core/chat/` | Canonical ChatMessage format, JSONL sessions, chat-loop constraints |
| `.vorch/specs/agents.md` | `core/agents/` | Agent schema, persistence, workspace lifecycle, archive-on-delete |
| `.vorch/specs/tools.md` | `core/tools/` | Tool metadata, allowlist filtering, provider definitions, dispatch |
| `.vorch/specs/storage.md` | `core/storage/` | Data-directory setup, settings persistence, prompt fragments |
| `.vorch/specs/skills.md` | `core/skills/` | Local skill metadata loading and prompt allowlist filtering |
| `.vorch/specs/server.md` | `server/` | RPC envelope, FastAPI app, SSE/WebSocket transport, static WebUI serving |
| `.vorch/specs/cli.md` | `cli/` | Local server lifecycle commands, targeting rules, status/logging contract |
| `.vorch/specs/webui.md` | `webui/` | Svelte app shell, API client, Chat/Agents views, queue behavior |
