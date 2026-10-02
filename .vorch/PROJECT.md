# Project Context

## Project

vBot is a self-hosted environment for working with AI Agents across conversations, Projects, and automated tasks. It brings together persistent Sessions, Agent identity and Memory, project context, and Tools for acting on the host system and connected services. Agents can work directly with the user or carry out tasks independently.

The runtime and shared state live on the server. WebUI, Desktop, CLI, and Channels provide different ways to interact with the same system.

## Architecture

**Stack:** Python 3.14 only (hatchling; Development -> Python version), FastAPI + WebSocket + SSE, Svelte (JS, no TypeScript), pywebview. Kernel uses asyncio; threads only where native libraries require them.

**Application:** Every installation is a packaged application owned by `cli/application/`: per-user Windows installations (server, server-desktop, desktop-client; native `vBot.exe` tray facade) and Linux server installations (systemd user unit), installed by `scripts/install.ps1` / `scripts/install.sh` from the signed packages CI publishes, and updated durably from their channel (`release`, or `main` from the rolling `main-build` prerelease). There is no source installation and no local build path: a source checkout is a development checkout updated with git. Desktop stays an independent accessor. Read `cli.md` and `cli/application.md` before changing this boundary.

**Layers:**
```
core/          <- Kernel (async). No HTTP, no UI.
server/        <- FastAPI + WS + SSE. Imports core/. RPC dispatch lives in server/rpc/.
webui/         <- Svelte frontend. Own package.json. Talks HTTP/WS/SSE only.
cli/           <- CLI accessor. Server lifecycle locally; all other domains via shared RPC client.
desktop/       <- pywebview shell. Imports nothing from the project - HTTP only.
```

Bare map references resolve under `.vorch/domain-maps/`. Each `core/<module>/` folder's main file is its public API (soft limit: 1000 lines/file). Exceptions: `core/debug` uses an `__init__.py` facade (`debug.md`); independent utilities in `core/utils` use leaf imports.

Split oversized source files into focused internal units behind the owning module and its public contract: a large file costs context and tokens on every inspection and edit, and cohesion alone does not justify keeping a multi-thousand-line file. Avoid arbitrary chunks, method-binding tables, or compressed formatting that merely obscure the same context.

**Transport:** Commands use `POST /api/rpc`, never WebSockets. `/ws` carries persistent app-wide server-push events; SSE streams each Run; logs and terminals use dedicated sockets. Binary transfers use dedicated HTTP endpoints. No auth (single-user-local). See `server.md`.

**Flow:** Accessors -> HTTP/WS/SSE -> server RPC handlers -> core (Providers, Models, Tools, Agents) -> external APIs. Agentic-only; no separate non-agentic streaming path.

Live voice (`live_voice` Task Model) keeps the provider call, delegated reasoning and app operations on the server; an accessor holds at most the call media and answers UI requests (`model_tasks/live.md`).

**Persistence:** Every SQLite database opens through the shared kernel `core/database/` (`database.md`): connection and journal policy, additive schema evolution with a migration ledger, and for canonical databases the data-store marker (`<data-dir>/data-store.json`), data snapshots, quarantine and auto-restore. Canonical Session history lives in normalized tables of `<data-dir>/sessions.db` with FTS search; a fork shares its source's history through lineage instead of copying it (`sessions.md`). Only explicit operator and update workflows create data snapshots (`<data-dir>/snapshots/`); normal Runtime startup and operation never copy a database.

**Tools:** Execute a call whose intended effect is clear even when it does not match the schema; refuse only genuinely ambiguous calls, before side effects, with the corrected call. Similarity alone is not intent for a mutation or an explicit target. Schemas, argument normalization/validation, concurrency: `tools.md`. Agent-facing design: `tools/designing-agent-tools.md`; review procedure and instruments: `.vorch/workflows/tool-review-workflow.md`.

**Extensions:** API 11 (`core/extensions/_declarations.API_VERSION`; what each version added: `extensions.md`). Canonical Sessions own binding, receipt and Run identity; Extension databases own domain state. The app provides a generic page bridge; Extensions own domain UI (`extensions.md`).

**Configuration:** Data directory `~/.vbot` owns `settings.json` and `.env`, a user-owned fallback credential snapshot. Process environment takes precedence; vBot never rewrites `os.environ`. Owning domains validate every user-editable JSON file before runtime use; public accessors configure Settings only through cataloged paths. See `settings.md`, `storage.md`, and `providers/connections.md` (Custom Provider credentials).

## Domain Maps

Read domain roots and task-relevant references under `.vorch/domain-maps/` as described in `AGENTS.md` -> Load context for the task. Maps orient you to owners, boundaries, contracts, source, and tests.

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
| recall.md | `core/recall/` | Recall backends, the shared Passage index (literal FTS and vectors), background semantic indexing |
| statistics.md | `core/statistics/` | Disposable SQLite projection, report RPC |
| usage.md | `core/usage/` | Durable Model request accounting, historical Usage import |
| memory.md | `core/memory/` | Pinned memory service, workspace memory files |
| settings.md | `core/settings/` | Settings schemas, validation, update sections |
| prompts.md | `core/prompts/` | System Prompt assembly, fragments, variables |
| attachments.md | `core/attachments/` | Blob storage, MIME sniffing, text extraction |
| extensions.md | `core/extensions/` | Extension kernel boundary, loading/lifecycle, management operations, live Tool catalogs, bundled MCP |
| agent.md | `core/agents/` | Agent schema, workspace lifecycle, archive and restore files |
| archive.md | `core/archive/` | Archive entries: archive on delete, restore, purge, retention, recovery |
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
| desktop.md | `desktop/` | pywebview shell contract, bridge, Desktop Voice, Live voice support |
| webui.md | `webui/` | Frontend accessor boundary, shared invariants |
| logging.md | cross-cutting | What to log at which level, line format, never-log rules, writing pipeline |
| logs.md | log viewer subsystem | Log parsing, RPC/socket contract, Logs tab |
| debug.md | `core/debug/` | Debug Mode, traces, redaction, recorder |
| performance.md | `core/performance/` | Always-on metrics, Event Loop stalls, Perfetto Recordings, metric catalog |

## Conventions

**Dependency injection:** Constructors (`__init__`) and `typing.Protocol` interfaces; no service locator or global singletons. Three exceptions: performance measurement records through module-level functions into one process-wide sink, like logging; it holds only measurements, never state that decides behavior (`performance.md`). Every server `httpx` or `httpx2` (the MCP SDK's HTTP client) client or transport, bundled Extensions included, passes `verify=shared_ssl_context()` (`core/utils/tls.py`), and every `websockets` `wss://` connection passes it as `ssl`: one process-wide context with httpx's default verification, prewarmed off the Event Loop at bootstrap, because a bare client or connection re-parses the CA bundle (150-250 ms, blocking) each time. The same startup thread imports the transport modules httpx loads lazily (httpcore, anyio's asyncio backend), which otherwise cost the first client up to 0.4 s on the Event Loop. `tests/core/utils/test_tls.py` guards every call site. The database kernel keeps process-wide state keyed by resolved path, because it describes what this process does to files, not an object graph: its tracked SQLite connections (quarantine, discard and restore refuse while one is open) and the data snapshot member freeze (`core/database/snapshot_barrier.py`). Every `Database.write` and JSON document write of a data directory passes that freeze, including standalone stores, offline tools and Extension databases no Runtime constructs; `write_json_document` callers have no injected object a per-Runtime freeze could travel through, and a gate keyed by path cannot be missed by an owner that was never handed it (`database.md` -> Snapshot consistency).

**Object IDs:** Use `core/utils/ids.py` for vBot-owned references: short type prefix + 12 lowercase base32 characters (60 random bits) with atomic owner-side uniqueness claims; 16 characters (80 bits) for pre-persistence/high-volume identities lacking a complete allocation catalog (Messages, Runs, Queue items). Storage and Tool results use the same id; no display aliases or abbreviated incoming ids (scoped exceptions: inside Live Tool calls, refs such as `s1`/`t1` that stay stable across calls, `model_tasks/live-operator.md`; in the Swarm, post, discussion and Wiki page numbers `#N`/`dN`/`wN`, which the Swarm Store resolves to its stored ids, `extensions/swarm.md`). Preserve existing opaque ids exactly. Path-backed readers validate safe basenames, not generator format; exact-id stores confirm the stored spelling with `has_id_entry` so case-insensitive filesystems cannot alias case variants. A user-chosen name that becomes one file or folder name (Agent, Project, Channel and Custom Provider ids, Skill names and new support files, custom prompt block slugs, a new id for an archive restore) must also pass `is_reserved_name` when it is created or renamed, on every platform, so the data stays usable on Windows: Windows device names such as `CON`, `NUL`, `COM0`-`COM9` or `LPT1`, also with an extension, plus names ending in a dot or space or containing `< > : " / \ | ? *` or control characters. Refuse with `reserved_name_message(subject, name)`; readers keep accepting existing names. `is_safe_id` includes the check. Owners authorize access; prefixes/randomness do not. Provider/protocol ids, credentials, hashes, and private storage generations keep their own contracts. Tests: `tests/core/utils/test_ids.py` and owner collision/round-trip tests.

**Errors:** Base classes in `core/utils/errors.py`, domain subclasses per module. Expected errors: handle locally, log `warn`; unexpected: rethrow, log `error`. Never silently swallow. Transient HTTP retries use shared `core/utils/retry.py` backoff and idempotency-aware statuses in `core/utils/http_status.py`. Chat owns ordinary Model retries through one Run-local recovery budget and suppresses nested utility retries for each Model attempt; other callers retain their retry policy. Provider errors are `retryable` or `fatal`.

**Logging:** Structured logs via `LogManager` (`core/utils/logging`), per-module `vbot.<domain>` loggers, `<data_dir>/logs/`. No `print()` or `logging.basicConfig()`. The INFO log alone must let an operator reconstruct what the server did: process start/stop with version and reason, each Run's terminal outcome, each material control-plane mutation (once, at its owner, with actor), and health degradation and recovery; per-step, per-call, per-attempt and per-poll detail is DEBUG. Never log credentials, content (Prompt, Skill, Memory, Model output, user text) or external ids. Before adding, removing or re-leveling a log line, read `logging.md`.

**Time:** Persist ISO 8601 UTC timestamps with explicit offset; database tables store the canonical fixed-width form `YYYY-MM-DDTHH:MM:SS.ffffffZ` (`core/utils/timestamps.py`, `database.md`). Optional IANA `timezone` defaults to the server host zone; once Settings resolve, it alone controls Agent context, wall-clock Calendar/Cron behavior, and UI rendering, never implicit browser/host time. No implicit `datetime.now()`.

**Persisted formats are stable (Generation 1).** Durable databases and JSON documents evolve compatibly: additive changes only (new tables, nullable or defaulted columns, optional JSON fields); readers tolerate and writers preserve unknown fields; data backfills are named, idempotent migrations recorded in the database, and a migration that older versions cannot read declares so. Renames, type or meaning changes, removals and new constraints require a new format generation with an explicit converter run by the updater or CLI, never by normal startup. App code knows only the current generation: no fallback keys or old-field branches. Details: `database.md` (SQLite) and `settings.md` -> JSON Document Contract.

**I18n:** WebUI text uses i18n: `webui/src/lib/i18n.js` with its English catalog (`webui/src/lib/i18n/`) as the only source of WebUI text (no call-site fallbacks). The backend, CLI and tray have no catalog and write user-visible text in English; the WebUI translates a server error by its code or structured fields where it shows its own wording.

**Model-facing paths:** Render separators as `/` when vBot authors a known filesystem-path value for Model context (System Prompt, attachment note, Tool result, delivery note). Leave `pathlib.Path`, native OS calls, persisted values, incoming arguments, and arbitrary text unchanged; no global replacement.

**Filesystem checks:** Since Python 3.14, `Path.exists`, `is_dir` and `is_file` return `False` when the check itself fails (permission or I/O error, an unresponsive share or cloud-file provider), so an entry that cannot be checked reads as missing. Where that answer decides about data (seeding, overwriting, deleting, restoring, adopting), use `exists_strict`, `is_dir_strict`, `is_file_strict` or `stat_or_none` from `core/utils/file_status.py`, which raise the `OSError` instead, or open the file and treat only `FileNotFoundError` as missing; an entry that cannot be checked is unavailable, never absent. The same module classifies unfollowed `lstat` results with `is_link_status` (symbolic links and junctions), for walking, verifying, moving and removing trees without following links; do not add local copies. Other Windows reparse points, such as OneDrive cloud-file placeholders, are ordinary files and directories everywhere, package verification included.

**Typing:** Every method that overrides a base class method, test doubles included, carries `@override` from `typing`; mypy's `explicit-override` error code requires it, so renaming or removing a base method fails the type check instead of silently orphaning its overrides. A hook looked up by name (`getattr(adapter, "hook", None)`) has no such protection. Generic classes, functions and methods declare type parameter lists (`class Box[T]:`, `def first[T](...)`) and aliases use `type` statements; Ruff's `UP` rules reject `TypeVar`, `Generic` and `TypeAlias`, except in files pinned to an interpreter older than 3.12. A `type` alias is a lazily evaluated `TypeAliasType`: runtime introspection (`get_args`, `isinstance`) needs its `__value__`.

## Development

**Setup:** Python 3.14 (Python version below), Node.js for WebUI (Node.js >=22 plus npm on the server for optional WhatsApp Channels); editable install with dev extras:
```bash
pip install -e ".[dev]"
python -m cli.search_runtime
git config core.hooksPath .githooks   # once per clone; worktrees share it
```
Use the current interpreter; do not assume a virtual environment for installs, checks, or runtime commands. That interpreter must run the SQLite the packages bundle, since its SQLite decides the journal mode of every database (`database.md`), so tests and measurements see what installations run: on Windows install the pinned library into it once from an elevated terminal (`python -m cli.application.runtime_sqlite`); on Linux use a Python whose SQLite runs WAL, such as the packages' runtime of `scripts/linux/python.lock.json`. `cli.application.runtime_sqlite.interpreter_problem` names a mismatch, and `scripts/push.py` (its `sqlite` step), `perf_bench.py` and `perf_load.py` refuse to run on one. Before editing the installers in `scripts/`, read [USAGE.md](../USAGE.md#installation) for end-user installation/update/removal and `cli/application.md`. `cli.search_runtime` provisions the private, digest-locked ripgrep executable the search Tools require; worktree creation, CI and packaging run it (`tools/search_files.md`).

**Python version:** vBot supports exactly one CPython minor version, the one its packages bundle (currently 3.14, `scripts/package_build.PYTHON_VERSION`). Development checkouts, mypy and every CI job use it too; there is no compatibility floor and no matrix across versions. Changing it is one deliberate commit that moves every pin together: `requires-python` and mypy's `python_version` in `pyproject.toml`, `PYTHON_VERSION` (the Windows builder's runtime check and the Linux runtime layout), the python-build-standalone runtime in `scripts/linux/python.lock.json`, all five runtime dependency locks (regenerated with `--python-version` and checked to resolve with `--only-binary :all:`), every workflow's and `.github/actions/*`'s `python-version`, and the setup documentation. `tests/scripts/test_package_build.py` fails while one of them disagrees. The managed local engine environments (STT, TTS, embeddings: `model_tasks/speech.md`, `model_tasks/embeddings.md`) follow it without a pin of their own: each runs on the server's `major.minor` unless its `[tool.vbot.*]` recipe in `pyproject.toml` names `python`, and setup recreates an environment whose Python differs (`python_changed`). Only Chatterbox's recipe names one, 3.13, because its dependency `spacy-pkuseg` publishes no wheels for newer Pythons (the recipe's comment says when to drop it). Two kinds of vBot files therefore run on other interpreters: `core/model_tasks/speech_worker.py`, which every speech environment runs, Chatterbox's on 3.13 (it imports only the standard library at startup, and vBot's source only for managed STT, which runs on the server's Python), and the bundled Skill scripts (`resources/skills/*/scripts/*.py`), which run on whatever `python` the host's `PATH` provides. Ruff's global `target-version` is `py314`; `[tool.ruff.per-file-target-version]` pins the speech worker to `py313` and the Skill scripts to `py39`, the oldest version each supports, so neither lint nor formatter introduces newer syntax there. Ruff sees only syntax: the commit hook and CI also run mypy `--python-version 3.13` over the speech worker (`scripts/commit_check.OLDER_PYTHON_FILES` and `OLDER_PYTHON_VERSION`), which rejects standard-library names added after 3.13. `tests/scripts/test_commit_check.py` fails while these pins differ from the oldest Python a speech environment runs; once every speech environment runs on the server's Python, drop the pins and the extra mypy check. Nothing checks the Skill scripts' standard-library use; keep it to what 3.9 offers.

A Python version change also checks that `sqlite3`'s `autocommit` default is still `LEGACY_TRANSACTION_CONTROL`: the database kernel relies on legacy `isolation_level=None` and issues its own `BEGIN IMMEDIATE`; `autocommit=True` is the forward spelling, but there `commit()` no longer ends an explicit `BEGIN` (`database.md` -> Conventions -> Transaction control).

**Worktrees:** `python scripts/worktree.py create|list|merge|delete <task-name>`; also `repair-start|repair-finish`. `create` reports path, ports, data dir and URL; it copies the primary checkout's matching WebUI packages and search engine instead of installing them and does not build the WebUI (`scripts/test-env.py start` does). `merge` reports a conflict with `main` first, then lands the task branch on `main` and removes the worktree, with a merge lock and protected conflict-repair window that hold until `main` has the merge. It runs no tests: it makes the merge commit in a private landing checkout (`.worktrees/.landing-*`), where the commit hook checks it, and fast-forwards `main` to it only once it passed, so a failing, conflicting or killed merge leaves `main` unchanged; later runs remove the private checkouts of dead merges and pushes and safely roll back a merge that an earlier version left staged in `main`. A merge that changes `webui/package-lock.json` runs `npm ci` for its check in the landing checkout and, once `main` has the merge, in `main`'s `webui/`. Cleanup removes data only with matching ownership records; `delete --force` discards uncommitted work. On failure/unexpected behavior, read `scripts/README-worktree.md`.

**Dependencies:** `pyproject.toml` groups include `server`, `cli`, `application`, `desktop`, `local-speech`, `local-tts`, and `dev`; frontend: `webui/package.json`. A change to the base dependencies or the `server`, `cli`, `application`, or `desktop` extras also regenerates the hashed package runtime locks: Windows (`scripts/windows/requirements.md`) and Linux (`scripts/linux/README.md`). The `desktop` Voice stack: `desktop.md` -> External Dependencies; optional local speech setup: `model_tasks/speech.md`; the local embedding engine's managed environment recipe (`[tool.vbot.local-embeddings.onnx]`, never a base or extra dependency): `model_tasks/embeddings.md` -> Local Engine. `sniffio` stays a direct dependency although vBot never imports it: httpcore probes it on every connection and stream, and each failed probe costs an import and `sys.path` scan (~0.4 ms of GIL time). Every third-party package vBot code imports is declared in the group of the importing code, even when another dependency already brings it (`anyio` for the bundled MCP and Computer Use Extensions, `httpx2` for MCP, `starlette` for the server); MCP protocol types are imported through the SDK's `mcp.types` mirror, not the `mcp_types` package it pins.

**Run:**
```bash
python server/main.py                 # Server foreground
python cli/main.py server start       # Server background (managed)
python desktop/main.py                # Desktop shell
```
A git-ignored checkout marker selects dev data `~/.vbot-dev`, port `8421`; the installed CLI outside the checkout uses `~/.vbot`, `8420`; managed worktrees have their own. Never target the installed instance with development commands, including its interpreter: running an installed version's runtime interpreter (`versions/<id>/runtime/python.exe`, `runtime/bin/python3` on Linux) writes `__pycache__` into the verified version (`cli/application.md` -> Update operation).

**Data store:** `python cli/main.py data-store status|snapshot|incident|unregister` reports and manages every canonical database (`cli.md`). A data directory from before Generation 1 (vBot 0.4.4 and earlier) has no converter and is refused at startup (`database.md` -> Evolution contract, item 6).

**Frontend build:** `cd webui && npm ci && npm run build`. It also compiles bundled Extension `ui/page.html` entries to relative `web/` assets (`webui/scripts/build-extension-pages.mjs`); installers ship assets and Extension sources. `npm run format`/`format:check`/`lint` and the commit hook cover these Extension sources too.

**Release:** Read `.vorch/workflows/release-workflow.md` when the user requests a release.

**Push:** Read `.vorch/workflows/push-workflow.md` when the user asks to push.

## Testing

Backend: pytest with `--import-mode=importlib`; frontend: Vitest, optionally jsdom when helper assertions cannot cover rendered components. Test directories mirror source packages (`tests/<package>/<module>/`, `webui/src/<module>/__tests__/`), so an owner's tests are easy to find. Inside a directory, backend test modules follow owners and behaviors, not source files: one module for an owner's public interface (`test_<owner>.py`), split by behavior area (`test_<owner>_<behavior>.py`) once it grows beyond about 1,000 lines. WebUI tests follow the same rule as `<Owner>.test.js` and `<Owner>.<behavior>.test.js` (Owner is the source file's basename, behavior kebab-case, one level); support-module naming: `webui/source-map.md`.

**Text assertions:** Exact strings only for stable contracts (protocol tokens, persisted formats, accessibility names, forbidden internal values) or test-owned transport sentinels. For editable prose, errors, and help, assert exception types, codes, structured fields, DOM roles, or security invariants instead. Evaluate wording in scenarios, not substring tests.

**Event Loop isolation:** Async tests and fixtures on a worker share one session-scoped Event Loop, so a task a test leaves behind keeps running during later tests. Close every `Runtime` started inside a test's Event Loop with `await runtime.aclose()`; the root `tests/conftest.py` fails a test that leaves one started, its performance monitor running, or a canonical database it opened still open. A wait until the loop is quiet covers only the tasks started since the test began (compare with `asyncio.all_tasks()` taken at its start), never every task on the loop: earlier tests' tasks may run far longer (Swarm `settled`). Never patch the process-wide `asyncio.sleep`: a module whose waits tests skip exposes a module-local `_sleep` seam (for example `core.utils.retry._sleep`), and tests patch that seam. `tests/core/conftest.py` skips the `core.utils.retry` backoff waits in every core test, and `tests/core/chat/conftest.py` records the Chat recovery backoff (`core.chat.recovery._sleep`) instead of waiting in every Chat test; a test that observes those waits patches the seam itself.

**Home isolation:** The root `tests/conftest.py` gives every test an empty home directory (`HOME` and `USERPROFILE`, `XDG_CONFIG_HOME` unset), so `Path.home()` and `~` never reach the real home, its `~/.vbot` data or its Git configuration. A value computed from the home at import time, such as `core.storage.storage.DEFAULT_DATA_DIR`, still names the real home: tests pass an explicit data directory.

**Disk syncs:** The root `tests/conftest.py` skips the disk syncs that protect data only against power loss: for the whole session, the database kernel opens every database with `synchronous=OFF` (the test seam `core.database.database.SYNCHRONOUS_OVERRIDE`) and `os.fsync` does nothing. A test that observes the production syncs (SQLite `synchronous` levels, `os.fsync` calls) carries the `durable` marker, which restores them for that test.

**Unreadable files:** The `deny_access(path)` fixture of `tests/core/conftest.py` simulates an entry that exists but cannot be checked or read, the same way on Windows and Linux: listing a denied folder, and `stat` or a reading open of a path inside it (or of a denied file), raise `PermissionError`, while `os.path.exists`/`isdir`/`isfile` answer `False` for them as Python 3.14 does; writes still succeed, so a regression that takes the entry for missing overwrites it visibly. `monkeypatch.undo()` lifts the denial before the test inspects the result.

**Running tests and checks:** Call the tools directly; their configuration lives in `pyproject.toml` (pytest, Ruff, mypy) and `webui/package.json` (scripts). What to test and run: `AGENTS.md` -> Testing.
```bash
python -m pytest tests/core/tools/test_bash.py         # file, directory, node id; -k/-x/--lf as usual
python -m pytest --durations=25 tests/core/chat        # plus the slowest tests
python -m pytest -m stress                             # load tests, excluded by default
python -m pytest tests/core/calendar --cov=core/calendar --cov-branch   # coverage of an owner
python -m ruff check --fix <paths>; python -m ruff format <paths>
python -m mypy                                         # configured project; seconds with a warm cache
cd webui && npx vitest run src/lib/__tests__/i18n.test.js
cd webui && npx vitest run src/lib/__tests__/*.guard.test.js   # repo-wide WebUI guards
cd webui && npm run build              # also: npm run lint, npm run format:check
```
pytest runs with work-stealing xdist and a 30 s per-test timeout; pass `-n 0` for a handful of tests or a debugger. Locally, every pytest run of every checkout claims its workers' cores from one pool per machine (all logical cores but two, at least the physical ones, `tests/cpu_pool.py`) and waits while they are busy, so parallel sessions queue instead of overloading the machine; `-n auto` means 2 workers there, an explicit `-n N` at most the pool's size. Each run appends a line to `~/.cache/vbot-test-cores/runs.jsonl` (checkout, cores, wait, duration, outcome). CI skips the pool and uses all cores. Load and scale variants (for example forty participants instead of three) carry the `stress` marker; the default options deselect them (`-m "not stress"`) in the suite, the push check and CI, and `-m stress` runs them. WebUI guard tests (`src/**/__tests__/*.guard.test.js`) scan every WebUI and Extension page source. The complete suites are `python -m pytest` and `npx vitest run`; the push check and CI run them.

**Commit hook:** `.githooks/pre-commit` and `.githooks/pre-merge-commit` run `scripts/commit_check.py` on every commit and merge commit, in every checkout. It runs the static checks on the staged files and no tests: Ruff fix, format and lint; mypy over the configured project plus staged Python files outside it, once for Windows and once for Linux (`--platform`, concurrently, each with its own cache); mypy `--python-version 3.13` over the speech worker, which a managed environment runs on Python 3.13 (`OLDER_PYTHON_FILES`), when it or `pyproject.toml` is staged, as CI's static job does on every run (Python version above); Prettier and ESLint for WebUI and Extension page `ui/` sources, and `npm run lint` and `format:check` over every source when the WebUI packages or the lint or format configuration change. It re-stages its fixes only for completely staged files and checks partially staged files as they are in the working tree. The WebUI checks block while `webui/node_modules` differs from `package-lock.json` (`scripts/_webui_packages.py` compares them); run `npm ci` in `webui/` then, as the hook never installs packages. A staged `package.json` that the lock does not record blocks until `npm install` in `webui/` updates the lock.
- Blocking: a mypy error blocks when it is in a staged file or in a file without uncommitted changes; one in unstaged or untracked work is reported without blocking. A mypy note goes with the error it follows; a note alone never blocks.
- The first mypy run in a new worktree builds its caches (~45 s).
- The `dev` extra pins Ruff and mypy exactly so the hook and CI agree; upgrade them deliberately, together with the fixes a new version requires.

**Push check:** `python scripts/push.py` checks the commit `main` points to in a private checkout (`.worktrees/.push-*`): Ruff, mypy for both platforms, the complete pytest suite on every core of the pool, and the WebUI's format check, lint, Vitest and build; on Windows also the complete pytest suite on Linux in WSL (`scripts/linux/push_tests.sh`, cache in WSL's `~/.cache/vbot-push`). It pushes exactly that commit to origin's `main`, as a fast-forward, only when every step passed (`--no-push` checks only). Backend tests that fail run once more alone; those that pass then are reported as flaky without blocking. The complete output goes to a log under `.git/vbot-push-logs/`.

**CI:** `ci.yml` runs the static checks and the complete pytest suite on Linux (x64 and ARM64) and Windows with the one supported Python version (Windows split into shards through `VBOT_TEST_SHARD`), the WebUI checks and E2E. The backend and E2E jobs run on the Python the packages bundle (`.github/actions/packaged-python`): on Linux the python-build-standalone runtime of `scripts/linux/python.lock.json`, on Windows the runner's CPython with the SQLite library of `scripts/windows/sqlite.lock.json`. Their SQLite decides the journal mode the databases run in (`database.md`), so the tests run in the mode installations use; the runners' own SQLite is older and would confine them to the rollback journal. `main-build.yml` calls it on every push to `main` that changes more than development documentation (`.vorch/`, the root guides), alongside the signed main-channel package builds, and publishes them to the `main-build` prerelease only once CI and every package passed; a newer push cancels the CI and package jobs of an older run. It is also the release gate (`release.yml`) and can be started by hand (`gh workflow run ci.yml`). Before the backend tests each job provisions what a test would otherwise fetch or warm inside its 30 s budget: the search engine, the token estimation encodings (tiktoken downloads them on first use) and, on Windows, the native host toolchain (a runner's first clang-cl build takes 20-30 s). A red run notifies the maintainer through GitHub; the release workflow checks the latest one on `main` first. Backend tests that fail run once more, alone: tests that then pass failed only under the parallel load and become a `Flaky tests` warning annotation and job-summary entry instead of failing the job; fix them like any failure. Read push runs with `gh run list --workflow=main-build.yml` and failures with `gh run view <run-id> --log-failed`.

**Performance:** Manual, outside the test suites and CI: `python scripts/perf_load.py` (concurrent-Agent load test), `python scripts/perf_bench.py` (hot-path microbenchmarks), and the always-on server metrics and Recordings (`vbot performance`, `performance.md`). Prove optimizations with `--compare` against a baseline from the same machine; read `scripts/README-perf.md` first.

## Live Testing

Before live tests, fully read `.vorch/workflows/web-test-workflow.md` for browser WebUI testing, `.vorch/workflows/cli-test-workflow.md` for CLI testing; both for tasks spanning both accessors.

## End-to-End Testing

Playwright `tests/e2e/` is excluded from the pytest and Vitest suites; the release gate requires it before publishing (`.github/workflows/e2e.yml`). Local runs require explicit user request and a full read of `.vorch/workflows/e2e-test-workflow.md` before every run.

## Context

Only strategic decisions or global constraints an Agent might otherwise misread belong here.

- **Platforms:** vBot targets Windows and Linux, with Windows currently prioritized. Keep shared components portable and platform-specific integration isolated. Server and accessors may run on the same machine or separate hosts. CI type-checks and tests on Linux as well, and the commit hook type-checks for both platforms on any host: use Windows-only standard-library names (`stat.IO_REPARSE_TAG_*`, `os.stat_result.st_reparse_tag`, `subprocess.CREATE_*`, `ctypes.WinDLL`, `winreg`, ...) only behind a `sys.platform == "win32"` check that mypy understands (an `if` statement or an early exit; not `os.name`, a conditional expression or a function-level `assert`), and check the other platform's view with `python -m mypy --platform linux` (or `win32`).
- **Durable JSON documents:** every JSON document an owner keeps in the data directory as durable state (configuration, operator state, stored metadata such as blob sidecars) follows the Generation 1 contract in `settings.md` -> JSON Document Contract (`format_version`, unknown fields kept, no overwrite after a failed load). It joins through `core.json_documents.DURABLE_DOCUMENTS` plus a `doctor config` validator and is thereby a data-snapshot member. The few JSON files outside the contract or outside snapshots are listed there.
- **Kernel-to-Model notifications:** Only sanctioned channels from `model-communication.md`: persisted notes rendered as System Reminders, System Prompt blocks, Tool definitions/results. Every domain (including Extensions, Channels, Tools, automation) must use these; never invent a channel.
