# Project Context

## Project

vBot is a self-hosted environment for working with AI Agents across conversations, Projects, and automated tasks. It brings together persistent Sessions, Agent identity and Memory, project context, and Tools for acting on the host system and connected services. Agents can work directly with the user or carry out tasks independently.

The runtime and shared state live on the server. WebUI, Desktop, CLI, and Channels provide different ways to interact with the same system.

## Architecture

**Stack:** Python 3.14 only (hatchling; Development -> Python version), FastAPI + WebSocket + SSE, Svelte (JS, no TypeScript), pywebview. Kernel uses asyncio; threads only where native libraries require them.

**Application:** Every installation is a packaged application owned by `cli/application/`: per-user Windows installations (server, server-desktop, desktop-client; native `vBot.exe` tray facade) and Linux server installations (systemd user unit), installed from the signed packages CI publishes and updated durably from their channel (`release`, or `main` from the rolling `main-build` prerelease). A source checkout is a development checkout updated with git; there is no source installation. Desktop stays an independent accessor. Read `cli.md` and `cli/application.md` before changing this boundary.

**Layers:**
```
core/          <- Kernel (async). No HTTP, no UI.
server/        <- FastAPI + WS + SSE. Imports core/. RPC dispatch lives in server/rpc/.
webui/         <- Svelte frontend. Own package.json. Talks HTTP/WS/SSE only.
cli/           <- CLI accessor, including the full-screen `vbot tui`. Server lifecycle locally; conversation and management through server contracts.
desktop/       <- pywebview shell. Imports nothing from the project - HTTP only.
```

Bare map references resolve under `.vorch/domain-maps/`. Each `core/<module>/` folder's main file is its public API (soft limit: 1000 lines/file); exceptions: `core/debug` uses an `__init__.py` facade (`debug.md`), and independent utilities in `core/utils` use leaf imports. Keep imports cheap: a package `__init__.py` that re-exports a large stack loads its exports on first access (`_EXPORT_MODULES` plus a module `__getattr__`), and a heavy third-party package that only one operation needs is imported where that operation first runs, so server import and `Runtime.start` pay for neither (why and the current cases: `performance.md` -> Import cost).

Split an oversized source file into focused internal units behind the owning module and its public contract: a large file costs context on every inspection and edit, and cohesion alone does not justify a multi-thousand-line file. Avoid arbitrary chunks, method-binding tables or compressed formatting that merely obscure the same context.

**Transport:** Commands use `POST /api/rpc`, never WebSockets; the one exception is a Terminal's live operator input and resize, which travel over its terminal socket, ordered with its output (`tools/terminal.md`). `/ws` carries app-wide server-push events, SSE streams each Run, logs and terminals use dedicated sockets, and binary transfers dedicated HTTP endpoints (`server.md`). The server runs in one Access Mode chosen by the host at start, enforced by one access guard in front of every route (`access.md`): `local` (this computer only, the default), `lan` (home network without login) or `authenticated` (login from the internet through paired Devices).

**Flow:** Accessors -> HTTP/WS/SSE -> server RPC handlers -> core (Providers, Models, Tools, Agents) -> external APIs. Agentic-only; one Chat loop, and every Model request streams (Chat, Compaction, Session titles, image analysis, Extension sampling). A completed `ProviderAdapter.send()` response is the emergency path, allowed only after the Provider refuses to stream (`providers.md` -> Interfaces).

**Persistence:** Every SQLite database opens through the shared kernel `core/database/` (`database.md`): connection and journal policy, additive schema evolution, and for canonical databases the data-store marker, data snapshots, quarantine and auto-restore. Canonical Session history lives in `<data-dir>/sessions.db`; a fork shares its source's history through lineage instead of copying it (`sessions.md`). Only explicit operator and update workflows create data snapshots, never normal startup or operation; configuration files get automatic configuration backups (`database.md` -> Configuration backup).

**Tools:** Execute a call whose intended effect is clear even when it does not match the schema; refuse only genuinely ambiguous calls, before side effects, with the corrected call. Similarity alone is not intent for a mutation or an explicit target. Schemas, argument normalization/validation, concurrency: `tools.md`. Agent-facing design and review: the `vbot-agent-tools` Skill.

**Extensions:** API 15 (`core/extensions/_declarations.API_VERSION`; what each version added: `extensions/management.md` -> API versions). Canonical Sessions own binding, receipt and Run identity; Extension databases own domain state. The app provides a generic page bridge; Extensions own domain UI (`extensions.md`).

**Configuration:** Data directory `~/.vbot` owns `settings.json` and `secrets.json`, the encrypted Secret Store for data-directory credentials and OAuth tokens (`secrets.md`). Process environment takes precedence; vBot never rewrites `os.environ`. Owning domains validate every user-editable JSON file before runtime use; public accessors configure Settings only through cataloged paths (`settings.md`, `storage.md`; Custom Provider credentials: `providers/connections.md`).

## Domain Maps

How to read the maps: `AGENTS.md` -> Load context for the task.

| Map | Domain | Covers |
|---|---|---|
| runtime.md | `core/runtime/` | Bootstrap, service lifecycle, DI wiring |
| providers.md | `core/providers/` | Provider boundary, Connection/discovery/request/usage invariants |
| models.md | `core/models/` | Model DB layers, registry, capabilities, id convention |
| model_tasks.md | `core/model_tasks/` | Task Model bindings and execution, target discovery, option schemas, Decision Model executor |
| chat.md | `core/chat/` | ChatMessage boundary, Agentic Loop invariants |
| runs.md | `core/runs/` | Run lifecycle, cancellation, timeline events, queues |
| compaction.md | `core/compaction/` | Triggers, strategies, plans, checkpoints |
| sessions.md | `core/sessions/` | Canonical SQLite Session persistence, metadata, and lifecycle |
| database.md | `core/database/` | SQLite kernel, format evolution, data-store marker, data snapshots, configuration backups, recovery |
| recall.md | `core/recall/` | Recall backends, Passage index (FTS and vectors), semantic indexing |
| statistics.md | `core/statistics/` | Disposable SQLite projection, report RPC |
| usage.md | `core/usage/` | Durable Model request accounting, historical Usage import |
| memory.md | `core/memory/` | Pinned memory service, workspace memory files |
| settings.md | `core/settings/` | Settings schemas, validation, update sections |
| json-documents.md | cross-cutting | Generation 1 contract of every durable JSON document |
| prompts.md | `core/prompts/` | System Prompt assembly, fragments, variables |
| attachments.md | `core/attachments/` | Blob storage, MIME sniffing, text extraction |
| extensions.md | `core/extensions/` | Extension kernel boundary, lifecycle, management, live Tool catalogs, bundled MCP |
| agent.md | `core/agents/` | Agent schema, Workspace lifecycle, archive files, built-in Agents |
| archive.md | `core/archive/` | Archive entries: archive on delete, restore, purge, retention, recovery |
| projects.md | `core/projects/` | Project boundary, anchor/ceiling invariants |
| subagents.md | `core/subagents/` | Sub-Agent tree, answer forwarding, tree control, reply targets |
| tools.md | `core/tools/` | Tool contracts and policy; index to per-tool maps |
| storage.md | `core/storage/` | Data-directory layout, temp-file lifecycle, persistence |
| secrets.md | `core/secrets/` | Secret Store, key protection, plaintext conversion |
| skills.md | `core/skills/` | Skill loading/validation, scopes, authoring, history and archive, Prompt-Epoch Catalog |
| automation.md | `core/automation/` | Cron/Bootstrap triggering, queue semantics, Reflection, Librarian passes |
| calendar.md | `core/calendar/` | Local events, recurrence, occurrence changes, Cron projection, calendar Tool |
| channels.md | `core/channels/` | Channel adapters, conversation engine, outbound send |
| model-communication.md | cross-cutting | Sanctioned kernel-to-Model channels; never invent one |
| server.md | `server/` | Transport/RPC boundary, events, source routing |
| access.md | `core/access/`, `server/access/` | Access Modes, access guard, Devices, pairing and login tickets, local access token, direct TLS |
| cli.md | `cli/` | Server lifecycle commands, targeting, output contract |
| desktop.md | `desktop/` | pywebview shell, bridge, server sign-in, Desktop Voice and dictation, Live voice |
| webui.md | `webui/` | Frontend accessor boundary, shared invariants |
| logging.md | cross-cutting | What to log at which level, line format, never-log rules, writing pipeline |
| logs.md | log viewer subsystem | Log parsing, RPC/socket contract, Logs tab |
| debug.md | `core/debug/` | Debug Mode, traces, redaction, recorder |
| performance.md | `core/performance/` | Always-on metrics, Event Loop stalls, Perfetto Recordings, metric catalog |
| development.md | `scripts/`, `.githooks/`, `.github/`, test infrastructure | Python version, dependency locks, commit hook, push check, CI, test core pool, writing tests |

## Conventions

**Dependency injection:** Constructors (`__init__`) and `typing.Protocol` interfaces; no service locator or global singletons. Three exceptions keep process-wide state: the performance sink, which like logging holds only measurements, never state that decides behavior (`performance.md`); the shared TLS context, which every server `httpx`/`httpx2` client or transport passes as `verify=shared_ssl_context()` and every `websockets` `wss://` connection as `ssl` (`core/utils/tls.py`, guarded by `tests/core/utils/test_tls.py`; why, and the variant for clients outside the server: `runtime/outbound-tls.md`); and the database kernel's tracked connections and snapshot freeze, keyed by path (`database.md` -> Process-wide state).

**Object IDs:** Use `core/utils/ids.py` for vBot-owned references: short type prefix + 12 lowercase base32 characters (60 random bits) with atomic owner-side uniqueness claims; 16 characters (80 bits) for pre-persistence/high-volume identities without a complete allocation catalog (Messages, Runs, Queue items). Storage and Tool results use the same id; no display aliases or abbreviated incoming ids, apart from scoped exceptions their maps define (Live refs `s1`/`t1`, `model_tasks/live.md`; Swarm numbers `#N`/`dN`/`wN`, `extensions/swarm.md`). Preserve existing opaque ids exactly. Path-backed readers validate safe basenames, not generator format; exact-id stores confirm the stored spelling with `has_id_entry`, so case-insensitive filesystems cannot alias case variants. A user-chosen name that becomes a file or folder name (Agent, Project, Channel and Custom Provider ids, Skill names and support files, prompt block slugs, archive restore ids) must pass `is_reserved_name` when it is created or renamed, on every platform, so the data stays usable on Windows: no device name such as `CON`, `NUL`, `COM0`-`COM9` or `LPT1` (also with an extension), no trailing dot or space, no `< > : " / \ | ? *` or control characters. Refuse with `reserved_name_message(subject, name)`; readers keep accepting existing names. `is_safe_id` includes the check. Owners authorize access; prefixes and randomness do not. Provider/protocol ids, credentials, hashes and private storage generations keep their own contracts. Tests: `tests/core/utils/test_ids.py` and owner collision/round-trip tests.

**Errors:** Base classes in `core/utils/errors.py`, domain subclasses per module. Expected errors: handle locally, log `warn`; unexpected: rethrow, log `error`. Never silently swallow. Transient HTTP retries use shared `core/utils/retry.py` backoff and idempotency-aware statuses in `core/utils/http_status.py`. Chat owns ordinary Model retries through one Run-local recovery budget and suppresses nested utility retries for each Model attempt; other callers retain their retry policy. Provider errors are `retryable` or `fatal`.

**Blocking work and cancellation:** Blocking in-process work leaves the Event Loop through named `BoundedWorkerPool`s, and owners that must finish admitted work before honoring cancellation use the shared helpers of `core/utils/workers.py` (`settle_before_cancelling`, `finish_despite_cancel`, `cancel_and_wait`) instead of their own `shield` or `wait` loops (`runtime/workers.md`).

**Logging:** Structured logs via `LogManager` (`core/utils/logging`), per-module `vbot.<domain>` loggers, `<data_dir>/logs/`. No `print()` or `logging.basicConfig()`. The INFO log alone must let an operator reconstruct what the server did: process start/stop with version and reason, each Run's terminal outcome, each material control-plane mutation (once, at its owner, with actor), and health degradation and recovery; per-step, per-call, per-attempt and per-poll detail is DEBUG. Never log credentials, content (Prompt, Skill, Memory, Model output, user text) or external ids. Before adding, removing or re-leveling a log line, read `logging.md`.

**Time:** Persist ISO 8601 UTC timestamps with explicit offset; database tables store the canonical fixed-width form `YYYY-MM-DDTHH:MM:SS.ffffffZ` (`core/utils/timestamps.py`, `database.md`). Optional IANA `timezone` defaults to the server host zone; once Settings resolve, it alone controls Agent context, wall-clock Calendar/Cron behavior, and UI rendering, never implicit browser/host time. No implicit `datetime.now()`.

**Persisted formats are stable (Generation 1).** Durable databases and JSON documents evolve compatibly: additive changes only (new tables, nullable or defaulted columns, optional JSON fields); readers tolerate and writers preserve unknown fields; data backfills are named, idempotent migrations recorded in the database, and a migration that older versions cannot read declares so. Renames, type or meaning changes, removals and new constraints require a new format generation with an explicit converter run by the updater or CLI, never by normal startup. App code knows only the current generation: no fallback keys or old-field branches. This governs how a change ships, not whether a design may change. Every JSON document an owner keeps in the data directory as durable state (configuration, operator state, stored metadata such as blob sidecars) joins `core.json_documents.DURABLE_DOCUMENTS` with a `doctor config` validator and is thereby a data-snapshot member. Details: `database.md` (SQLite) and `json-documents.md` (JSON, including the few files outside the contract or outside snapshots).

**I18n:** WebUI text uses i18n: `webui/src/lib/i18n.js` with its English catalog (`webui/src/lib/i18n/`) as the only source of WebUI text (no call-site fallbacks). The backend, CLI and tray have no catalog and write user-visible text in English; the WebUI translates a server error by its code or structured fields where it shows its own wording.

**Model-facing paths:** Render separators as `/` when vBot authors a known filesystem-path value for Model context (System Prompt, attachment note, Tool result, delivery note). Leave `pathlib.Path`, native OS calls, persisted values, incoming arguments, and arbitrary text unchanged; no global replacement.

**Filesystem checks:** In Python 3.14, `Path.exists`, `is_dir` and `is_file` return `False` when the check itself fails (permission or I/O error, an unresponsive share or cloud-file provider). Where that answer decides about data (seeding, overwriting, deleting, restoring, adopting), use `exists_strict`, `is_dir_strict`, `is_file_strict` or `stat_or_none` from `core/utils/file_status.py`, which raise the `OSError`, or open the file and treat only `FileNotFoundError` as missing: an entry that cannot be checked is unavailable, never absent. Walk, verify, move and remove trees without following links through its `is_link_status` (symbolic links and junctions); do not add local copies. Other Windows reparse points, such as OneDrive cloud-file placeholders, are ordinary files and directories everywhere, package verification included.

**Line endings:** Text files use LF, Windows PowerShell scripts (`*.ps1`) CRLF; `.gitattributes` enforces this regardless of each machine's `core.autocrlf`, `.editorconfig` tells editors, and the commit hook's Ruff and Prettier write LF. Git's `CRLF will be replaced by LF` warning on `git add` is harmless and needs no manual conversion. Python code that writes text files passes `newline="\n"` (`atomic_write_text` does), because text mode writes `\r\n` on Windows.

**Typing:** Every method that overrides a base class method, test doubles included, carries `@override` from `typing` (mypy's `explicit-override`), so renaming or removing a base method fails the type check instead of orphaning its overrides; a hook looked up by name (`getattr(adapter, "hook", None)`) has no such protection. Generic classes, functions and methods declare type parameter lists (`class Box[T]:`, `def first[T](...)`) and aliases use `type` statements; Ruff's `UP` rules reject `TypeVar`, `Generic` and `TypeAlias` except in files pinned to an interpreter older than 3.12. A `type` alias is a lazily evaluated `TypeAliasType`: runtime introspection (`get_args`, `isinstance`) needs its `__value__`.

## Development

**Setup:** Python 3.14 (Python version below), Node.js for WebUI (Node.js >=22 plus npm on the server for optional WhatsApp Channels); editable install with dev extras:
```bash
pip install -e ".[dev]"
python -m cli.search_runtime          # private ripgrep for the search Tools (tools/search_files.md)
git config core.hooksPath .githooks   # once per clone; worktrees share it
```
Use the current interpreter; do not assume a virtual environment for installs, checks or runtime commands. It must run the SQLite the packages bundle; how to set that up, and why: `development.md` -> Interpreter and SQLite. Before editing the installers in `scripts/`, read [USAGE.md](../USAGE.md#installation) and `cli/application.md`.

**Python version:** vBot supports exactly one CPython minor version, the one its packages bundle (currently 3.14); there is no compatibility floor. `core/model_tasks/speech_worker.py` (3.13) and the bundled Skill scripts (`resources/skills/*/scripts/**/*.py`, 3.9 and later) run on older interpreters: keep to their syntax and standard library. Before touching these files' interpreter features or changing any version pin, read `development.md` -> Python version.

**Worktrees:** `python scripts/worktree.py create|list|merge|delete <task-name>`; also `repair-start|repair-finish`. `create` prints path, ports, data dir and URL and does not build the WebUI (`scripts/test-env.py start` does); `merge` runs no tests and moves `main` only once the merge commit passed the commit hook. Never link a folder of another checkout into a worktree (such as a junction to the primary `webui/node_modules`): Git for Windows' `git worktree remove` follows junctions and deletes the target's contents; `create` installs a worktree's own dependencies, other tooling runs `npm ci` in its `webui/`. Conflicts: `.vorch/workflows/worktree-workflow.md`; mechanics, failures and unexpected behavior: `scripts/README-worktree.md`.

**Dependencies:** `pyproject.toml` groups include `server`, `cli`, `application`, `desktop`, `local-speech`, `local-tts` and `dev`; frontend: `webui/package.json`. Declare every third-party package vBot code imports in the group of the importing code, even when another dependency already brings it (`anyio` for the bundled MCP Extension, `httpx2` for MCP, `starlette` for the server). Before changing dependencies, read `development.md` -> Dependencies: some changes also regenerate the package runtime locks.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```
A git-ignored checkout marker selects dev data `vBot-data/dev` beside the repository root and port `22333`; the installed CLI outside the checkout uses `~/.vbot` and `22332`; managed worktrees get `vBot-data/<name>` (why outside the profile and the repository: `scripts/README-worktree.md` -> Basic model). Never target the installed instance with development commands (the one exception is Local deploy below), including its runtime interpreter (`versions/<id>/runtime/python.exe`, `runtime/bin/python3` on Linux), which writes `__pycache__` into the verified version (`cli/application.md` -> Update operation).

**Data store:** `python cli/main.py data-store status|snapshot|config-backup|incident|unregister` reports and manages every canonical database (`cli/data-store.md`). A data directory from before Generation 1 (vBot 0.4.4 and earlier) has no converter and is refused at startup (`database.md` -> Evolution contract, item 6).

**Frontend build:** `cd webui && npm ci && npm run build`; it also compiles bundled Extension `ui/page.html` entries to relative `web/` assets (`webui/scripts/build-extension-pages.mjs`), and `npm run format`/`format:check`/`lint` and the commit hook cover those Extension sources too.

**Release:** Read `.vorch/workflows/release-workflow.md` when the user requests a release.

**Local deploy:** When the user wants the current state in their installed vBot, commit, then run `python scripts/deploy_local.py` (no push needed): it builds the committed HEAD into the installation's package and installs it through the installation's own `update --package` (`cli/application.md` -> Installers and published packages).

**Push:** Read `.vorch/workflows/push-workflow.md` when the user asks to push, and push only that way (`python scripts/push.py`, never a plain `git push`). A push is finished only when the `main-build` CI run it starts is green.

## Testing

Backend: pytest with `--import-mode=importlib`; frontend: Vitest, optionally jsdom when helper assertions cannot cover rendered components. Test directories mirror source packages (`tests/<package>/<module>/`, `webui/src/<module>/__tests__/`). Inside a directory, backend test modules follow owners and behaviors, not source files: `test_<owner>.py` for an owner's public interface, split into `test_<owner>_<behavior>.py` beyond about 1,000 lines; WebUI tests likewise as `<Owner>.test.js` and `<Owner>.<behavior>.test.js` (Owner is the source file's basename, behavior kebab-case, one level; support modules: `webui/source-map.md`).

Before writing or changing a test, read `development/testing.md`: what to assert, how to wait, and what the shared fixtures isolate (Event Loop, home, temporary files, disk syncs, unreadable files).

**Running tests and checks:** Call the tools directly (configuration: `pyproject.toml`, `webui/package.json`); what to test: `AGENTS.md` -> Testing.
```bash
python -m pytest tests/core/tools/test_shell.py        # file, directory, node id; -k/-x/--lf as usual
python -m pytest --durations=25 tests/core/chat        # plus the slowest tests
python -m pytest -m stress                             # load tests, excluded by default
python -m pytest tests/core/calendar --cov=core/calendar --cov-branch   # coverage of an owner
python -m ruff check --fix <paths>; python -m ruff format <paths>
python -m mypy                                         # configured project; seconds with a warm cache
cd webui && npx vitest run src/lib/__tests__/i18n.test.js
cd webui && npx vitest run src/lib/__tests__/*.guard.test.js   # guards over every WebUI and Extension page source
cd webui && npm run build              # also: npm run lint, npm run format:check
```
pytest runs with work-stealing xdist and a 30 s per-test timeout; pass `-n 0` for a handful of tests or a debugger. Locally, the pytest runs of all checkouts share one pool of cores and queue while it is busy (`development.md` -> Test core pool). Load and scale variants carry the `stress` marker, which the suite, the push check and CI deselect. The complete suites are `python -m pytest` and `npx vitest run`; the push check and CI run them.

**Commit hook:** `.githooks/pre-commit` and `.githooks/pre-merge-commit` run `scripts/commit_check.py` on every commit and merge commit, in every checkout: static checks of the staged files (Ruff, mypy for Windows and Linux, Prettier and ESLint for WebUI and Extension page `ui/` sources, domain-map checks) and no tests. The WebUI checks block while `webui/node_modules` differs from `package-lock.json`; run `npm ci` in `webui/` then, as the hook never installs packages. The first mypy run in a new worktree builds its caches (~45 s). Re-staging, partial staging, when a mypy error blocks, and before changing the hook, its checks or the authorship enforcement: `development.md` -> Commit hook.

**CI:** `ci.yml` runs the static checks, the complete suites on Linux and Windows and the WebUI checks; `main-build.yml` runs it on every push to `main` and publishes the main-channel packages once it passed, and it gates releases. Before changing the push check, the CI workflows or the test core pool, read `development.md`.

**Performance:** Manual, outside the test suites and CI: `python scripts/perf_load.py` (concurrent-Agent load test), `python scripts/perf_bench.py` (hot-path microbenchmarks), and the always-on server metrics and Recordings (`vbot performance`, `performance.md`). Prove optimizations with `--compare` against a baseline from the same machine; read `scripts/README-perf.md` first.

## Live Testing

Before live tests, fully read `.vorch/workflows/web-test-workflow.md` for browser WebUI testing, `.vorch/workflows/cli-test-workflow.md` for CLI testing; both for tasks spanning both accessors.

## End-to-End Testing

Playwright `tests/e2e/` is excluded from the pytest and Vitest suites; neither CI nor the release gate runs it. The maintainer starts it occasionally, locally or by dispatching `.github/workflows/e2e.yml`. Local runs require explicit user request and a full read of `.vorch/workflows/e2e-test-workflow.md` before every run.

## Context

Only strategic decisions or global constraints an Agent might otherwise misread belong here.

- **Platforms:** vBot targets Windows and Linux, with Windows currently prioritized. Keep shared components portable and platform integration isolated; server and accessors may run on separate hosts. The commit hook and CI type-check for both platforms on any host: use Windows-only standard-library names (`stat.IO_REPARSE_TAG_*`, `os.stat_result.st_reparse_tag`, `subprocess.CREATE_*`, `ctypes.WinDLL`, `winreg`, ...) only behind a `sys.platform == "win32"` check that mypy understands (an `if` statement or an early exit; not `os.name`, a conditional expression or a function-level `assert`), and check the other platform's view with `python -m mypy --platform linux` (or `win32`).
- **New Model defaults:** listed chat Models remain selectable with inferred Provider/Connection/family defaults; lack of per-version verification and free-Model naming are not exclusion reasons. Exact catalog facts and concrete corrective exceptions take precedence (`providers.md` -> Defaults and concrete exceptions; user decision 2026-10-15).
- **Kernel-to-Model notifications:** Only sanctioned channels from `model-communication.md`: persisted notes rendered as System Reminders, System Prompt blocks, Tool definitions/results. Every domain (including Extensions, Channels, Tools, automation) must use these; never invent a channel.
