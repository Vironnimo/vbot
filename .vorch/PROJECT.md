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

**Dependency groups:** `server`, `cli`, `desktop`, `dev`. Core dependency: `httpx`. See `pyproject.toml` for exact packages.

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

## Specs

Domain-specific documentation lives in `.vorch/specs/`. A **domain** is any module or subsystem that has its own folder or clear boundary in the codebase — a chunk of code that has a distinct responsibility and that agents need context about before touching it. This includes technical modules (`hooks`, `tools`, `storage`), infrastructure modules (`server`, `channel`), and business modules (`auth`, `payments`). Size doesn't matter — what matters is that working on it without context risks misunderstanding its interfaces or conventions.

**When working on a domain: read its spec file.** Your task will list which specs are relevant — treat that as a starting point, not a ceiling. Read additional specs if you need them.

| Spec file | Domain | What it covers |
|---|---|---|
| `.vorch/specs/runtime.md` | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| `.vorch/specs/providers.md` | `core/providers/` | Provider config, adapter hierarchy, wire protocols, error classification |
| `.vorch/specs/models.md` | `core/models/` | Model data classes, registry, capabilities, model ID convention |
