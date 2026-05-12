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
+ `/ws/logs` (selected log-file live tail) + SSE (streaming). No auth
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
runtime skill registry immediately.

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
python -m venv .venv
.\.venv\Scripts\activate   # Windows
pip install -e ".[dev]"
```

**Dependency groups:** `server`, `cli`, `desktop`, `dev`. Core dependencies: `httpx` and `pyyaml` (direct `SKILL.md` YAML frontmatter parsing). The `server` group includes `watchfiles` for the dedicated log-view watcher transport. The `cli` group includes `psutil` for safe local process lookup during server lifecycle management. The `dev` group includes server transport dependencies, the log-view watcher dependency, and CLI process-management dependencies so backend quality gates exercise FastAPI/SSE/WebSocket and CLI tests. See `pyproject.toml` for exact packages.

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
keys), `settings.json`, `logs/`, OAuth tokens under `oauth/`, and all runtime
data.

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

- The Toasted `Components` showcase is a design/reference artifact only. It must
  not ship as a live WebUI tab.
- **Two-channel transport architecture:** SSE is the per-Run streaming channel
  (token-by-token output for one Run). WebSocket is the persistent app-wide
  signalling channel (connection status, agent CRUD, run lifecycle summaries).
  SSE and WS serve different purposes and should not be merged.
- **WebSocket is server-push only.** Clients send requests via `POST /api/rpc`.
  The WS channel broadcasts server events; it does not accept client commands.
- **WebSocket reconnect uses `after_sequence` replay.** Clients send the last
  sequence number they saw, and the server replays missed events.
- **Logs view transport is file-backed and isolated.** The WebUI log tab reads
  daily files from `<data_dir>/logs/` through dedicated `log.list`/`log.read`
  RPC methods plus `/ws/logs` for live updates of one selected file. `log.read`
  returns a short-lived cursor so the log socket can replay anything appended
  between the initial file read and websocket connect. It does not reuse the
  shared app event bus.
- **Logs view filtering and ordering stay local.** The WebUI Logs tab loads one
  selected daily file, then applies level filtering, text search, and
  newest/oldest ordering in-memory without re-reading the file for those UI
  controls.
- **Provider usability for model selection is credential-based, not
  health-based.** A provider is considered usable when its configured
  credential is present and non-empty. Missing or empty credentials mean the
  provider is ignored by model-selection UI. This applies to local providers
  too (for example, Ollama or LM Studio): no special-casing, no runtime
  reachability checks.
- **Provider credentials are source-agnostic.** Process environment currently
  has higher precedence than the data-dir `.env`, but backend code should ask a
  central provider-credential path whether credentials exist and what value to
  use, rather than reading `os.environ` directly.
- **OAuth provider credentials are token-store based when an OAuth block is
  configured.** `type: "oauth"` connections with an `oauth` block, or without an
  `auth.credential_key`, read persisted tokens from `<data_dir>/oauth/` through
  the central provider credential path. Existing OAuth stubs with a
  `credential_key` and no OAuth metadata continue to resolve through
  environment or data-dir `.env` credentials.
- **OAuth Device Flow is server-side.** Clients request a provider connection,
  display the returned user code and verification URL, and wait for a WebSocket
  event. Polling, token exchange, token refresh, and token persistence stay in
  backend provider code; token values must never appear in logs or public event
  payloads.
- **GitHub Copilot OAuth has provider-specific exchange requirements.** The
  working Copilot path uses GitHub Device Flow scope `read:user`, then exchanges
  the GitHub OAuth token at `https://api.github.com/copilot_internal/v2/token`
  with `Authorization: Bearer <github_oauth_token>` plus Copilot integration
  headers. `Authorization: token ...` is rejected by the Copilot exchange.
- **Provider connection identifiers in public RPC/UI payloads are compositional.**
  Use `<provider_id>:<connection.id>` (for example `github-copilot:oauth`) for
  `connection_id` values in Settings, provider RPC methods, and WebSocket
  events. Backend internals may derive the provider-local connection ID only
  after validating that prefix.
- **Model catalogs can be generated from provider APIs.** Dynamic refresh writes
  provider model files under `resources/models/` and may include `source` and
  `fetched_at` metadata that `ModelRegistry.load()` ignores. Optional
  `resources/models/<provider>.overrides.json` files patch or supplement
  discovered models, and the registry skips those override files when loading
  catalogs.
- **GitHub Copilot model discovery is tolerant OpenAI-compatible discovery, not
  OpenRouter discovery.** Copilot `/models` entries may omit `architecture` or
  provide it as a non-object. Do not route Copilot through OpenRouter's strict
  schema normalizer.
- **GitHub Copilot GPT-5 reasoning requests follow the OpenAI-style path.**
  Copilot GPT-5-family reasoning models use `reasoning_effort` and
  `max_completion_tokens`, not OpenRouter's `reasoning` object. When Copilot
  returns readable reasoning text inside `reasoning_details`, adapters must
  surface that text as visible `reasoning` while keeping the full raw
  `reasoning_details` payload private in `reasoning_meta` for round-tripping.
- **Model discovery strategy is config-driven and separate from request
  adapter selection.** `ProviderConfig.adapter` chooses the request/streaming
  protocol implementation. `ProviderConfig.model_discovery` chooses how
  `/models` responses are normalized. OpenRouter now declares an explicit
  `openrouter` discovery strategy; normal OpenAI-compatible providers use the
  generic `openai_compatible` strategy. The generic discovery path must ignore
  OpenRouter-only enrichment fields entirely so provider-specific schema cannot
  leak back into the shared path. Only implemented discovery strategies may
  auto-default; providers without one keep `model_discovery` empty instead of
  advertising an unsupported refresh path.
- **Token usage flows from providers through to the frontend.** Adapters extract `input_tokens`/`output_tokens` from provider responses (OpenAI: `prompt_tokens`/`completion_tokens`; Anthropic: `input_tokens`/`output_tokens`). Usage is persisted on assistant messages in JSONL sessions. The `run_completed` event includes usage in its payload. If a provider doesn't supply usage, the backend falls back to a 4-chars-per-token estimation and marks it with `"estimated": true`. Normal request-history serialization strips `usage`, `reasoning`, and `reasoning_meta` from prior assistant messages before sending them to providers, but same-turn tool-loop replay keeps the current assistant message's readable `reasoning` plus opaque `reasoning_meta` and still strips `usage`.
- **System reminders are kernel-internal notes.** Chat sessions may persist `role: "note"` entries for background events. The chat loop embeds them into provider requests as synthetic user messages wrapped in `<system-reminder>` tags; provider adapters must never receive `role: "note"`, and the normal UI should not present notes as user messages.
- **Skill catalogs expose no local paths.** The prompt-visible `<available_skills>` block contains only each skill's `name` and `description`; local `SKILL.md` paths are internal registry data used by activation code. Invalid or partially valid skill directories should remain visible through diagnostics so the WebUI can explain why a skill is unavailable.
- **Skill activation is session-scoped.** Skills may be activated through the internal `skill` tool or deterministic `/skill-name` and `$skill-name` message triggers. Activated `<skill_content>` is persisted as an internal note and restored on later provider requests in the same Session, while normal history/UI responses continue to hide note messages.
- **Programmatic run triggers queue in memory only.** Automation triggers can start runs without a WebUI send flow; when a target Session already has an active Run, triggers are queued FIFO in memory and are not persisted across process restarts.

## Specs

Domain-specific documentation lives in `.vorch/specs/`. A **domain** is any module or subsystem that has its own folder or clear boundary in the codebase — a chunk of code that has a distinct responsibility and that agents need context about before touching it. This includes technical modules (`hooks`, `tools`, `storage`), infrastructure modules (`server`, `channel`), and business modules (`auth`, `payments`). Size doesn't matter — what matters is that working on it without context risks misunderstanding its interfaces or conventions.

**When working on a domain: read its spec file.** Your task will list which specs are relevant — treat that as a starting point, not a ceiling. Read additional specs if you need them.

| Spec file | Domain | What it covers |
|---|---|---|
| `.vorch/specs/runtime.md` | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| `.vorch/specs/providers.md` | `core/providers/` | Provider config, adapter hierarchy, wire protocols, error classification |
| `.vorch/specs/models.md` | `core/models/` | Model data classes, registry, capabilities, model ID convention |
| `.vorch/specs/chat.md` | `core/chat/` | Canonical ChatMessage format, JSONL sessions, chat-loop constraints |
| `.vorch/specs/agent.md` | `core/agents/` | Agent schema, persistence, workspace lifecycle, archive-on-delete |
| `.vorch/specs/tools.md` | `core/tools/` | Tool metadata, allowlist filtering, provider definitions, dispatch |
| `.vorch/specs/storage.md` | `core/storage/` | Data-directory setup, settings persistence, prompt fragments |
| `.vorch/specs/skills.md` | `core/skills/` | Local skill metadata loading and prompt allowlist filtering |
| `.vorch/specs/automation.md` | `core/automation/` | Programmatic run triggering and in-memory queue semantics |
| `.vorch/specs/server.md` | `server/` | RPC envelope, FastAPI app, SSE/WebSocket transport, static WebUI serving |
| `.vorch/specs/cli.md` | `cli/` | Local server lifecycle commands, targeting rules, status/logging contract |
| `.vorch/specs/desktop.md` | `desktop/` | pywebview thin-client contract, target URL, window lifecycle, local settings |
| `.vorch/specs/webui.md` | `webui/` | Svelte app shell, API client, Chat/Agents views, queue behavior |
| `.vorch/specs/logs.md` | log viewer subsystem | Daily log parsing, log RPC/socket contract, WebUI Logs tab behavior |
