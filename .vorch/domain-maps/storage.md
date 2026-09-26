# Storage

Data-directory bootstrap, temporary-file lifecycle, atomic settings and credential persistence, and raw prompt-fragment file access.

## Overview

`core/storage/` owns the process-local filesystem services and the canonical placement contract for the runtime data root. It resolves the data directory (explicit value -> `DATA_DIR`/`VBOT_DATA_DIR` -> config -> checkout markers -> `~/.vbot`; a `cwd_only` marker applies only in its own step, letting a source checkout carry a dev data dir without redirecting installed CLIs), initializes the complete canonical structure, manages categorized temporary files, and reads/writes `settings.json`, the data-dir `.env`, prompt fragments, and System Prompt block persistence. Storage owns placement but not schemas (`core/settings/`), prompt assembly rules (`core/prompts/`), or domain records stored in those directories; generated images are caller-owned Workspace/Project files outside the layout.

## Canonical layout

`DataDirectoryLayout` is the immutable path contract: `artifacts/{attachments,speech,models,debug,performance,temp/{atomic,bash,subagents,terminals,web_fetch}}` (plus `artifacts/live-calls/`, which Live voice creates on first write), `statistics/` (the Statistics-owned disposable SQLite), and independent roots `agents`, `archive`, `bootstrap`, `calendar`, `channels`, `cron`, `extensions`, `logs`, `oauth`, `processes`, `projects`, `prompts`, `recall`, `skills`, `terminals`. Canonical databases such as `<data-dir>/sessions.db`, the Provider-owned `<data-dir>/provider-usage.db` and the Channel-owned `<data-dir>/channels.db` belong to their owning domains; the data-store marker `<data-dir>/data-store.json` (`DataDirectoryLayout.data_store_marker_path`), the maintenance guard `data-maintenance.json`, the operation lock `data-store.lock`, and the fixed `snapshots/`, `incidents/` and `quarantine/` roots belong to the database kernel (`database.md`), which creates them on demand. Initialization creates the full set non-destructively - `.env.example` copies only when `.env` is absent, empty settings only when missing; creation failures in the canonical directory are fatal. Concurrent initializers of one root are tolerated: only the call whose `mkdir` created the root writes the bootstrap marker listing no databases (`core.database.marker.write_bootstrap_marker`; a write failure is fatal), a call losing that race treats the root as existing, and a canonical subdirectory created concurrently counts as existing while a non-directory at a canonical path still raises `NotADirectoryError` (`test_layout.py`).

`DataDirectoryLayout.decisions_db` places durable Jev experiments and evaluation history at `<data-dir>/decisions.db`, a canonical kernel database (`database.md`); schema, revision checks, and lifecycle belong to Task Models (`model_tasks/decisions.md`).

Callers that require a fresh root use `initialize_data_directory(require_new=True)`: an existing root, including one created concurrently, raises `FileExistsError` before any seeding. Worktree creation uses this mode; its separate ownership records govern cleanup (`scripts/README-worktree.md`).

Directory creation does not transfer ownership: Agents own their trees, Channels theirs, Cron/Bootstrap their job stores, Attachments/Speech their artifacts, Performance its Recordings (`performance.md`), Live voice its call records (`model_tasks/live.md`), Models the Model DB, Statistics its read model, Terminal Manager launch history. Layout changes belong in `layout.py`; format/retention changes stay with owning domains.

Current code reads only the canonical paths; there is no dual-read of an older layout. Older data directories are converted once, offline, by the Generation 1 converter (`database/generation-1-conversion.md`), which stages inside the data directory as `generation-1-staging/` and keeps every replaced file under `pre-generation-1/`.

Runtime creates `<data-dir>/extension-data/<owner>/` for a loaded Extension's persistent host state. It is separate from `extensions/`, which contains executable overrides, and is not temporary-file cleanup data. Databases an Extension opens through `host.open_database` live there as canonical kernel databases `<name>.db` (`database.md`, `extensions.md`). The Extension owns its format; canonical temporary Session bindings, delivery receipts and Run ownership remain in `sessions.db` (`extensions.md`, `sessions.md`).

The archived Browser Use Extension previously owned `artifacts/browser-use/` (including its `refs.db` counter) and `~/.agent-browser/browsers`. Removal from bundled discovery does not delete these existing files or make them Storage cleanup categories; see `extensions/browser-use.md`.

## Temporary files

`TemporaryFileManager` owns only retained categories under `artifacts/temp/`: `bash`, `subagents`, `terminals`, `web_fetch`. Web Fetch snapshots retain for 72 hours; their Session/Agent ownership and saved-view format belong to the Tools owner. New retained filenames use `tmp_` plus 12 lowercase base32 characters and the owner-selected suffix; exclusive file creation retries collisions without truncating existing logs (`temp_files.py`, `test_temp_files.py`). Leases protect active files; idempotent completion starts retention (72 h bash/terminals, 24 h subagents); cleanup runs at start and every 60 s, removing only expired inactive regular files. With a running Event Loop every sweep, the first included, runs on the `temporary-files` worker pool, never on the loop (a sweep lists and stats every retained file: seconds on a busy disk); a start without a loop sweeps once inline. `stop` ends an in-flight sweep after its current file and `aclose` awaits it. The `atomic` child is short-lived staging removed by producers, not a retention category. This lifecycle is internal - Extensions get no temporary-file API.

## Settings & credentials

- `<data_dir>/settings.json`: raw I/O, validation-gated loading, normalized persistence helpers and locked transactions remain in `StorageManager`. Internal `_settings_updates.py` applies pure merges to the transaction-owned mapping; schemas and the shared Subagent defaults stay in `core/settings/`. `load_settings` serves an unchanged file version from memory: the stat stamp `(mtime_ns, size, inode)` is taken before reading, a file modified within the last 3 s is always re-read (writes inside one timestamp tick), `save_settings` invalidates, and every call returns an independent parsed copy. `save_settings` writes through `write_json_document`: unknown keys survive, and a file that fails to load is never overwritten (JSON Document Contract in `settings.md`). External edits therefore stay live without restarting.
- `patch_settings` owns the strict raw snapshot, known-field candidate and optional runtime validation within the existing Settings lock. The path owner uses that snapshot only to preserve unknown fields when deciding whether an emptied ancestor can be pruned; runtime validation still runs on known Settings before the single write. Section updates continue through `update_settings_sections`.
- `<data_dir>/.env` is a user-owned credential fallback: snapshot reads plus single-key updates; process environment keeps higher precedence when callers merge. Read failures log and yield an empty snapshot rather than blocking startup.
- Credential writes validate shell-style keys, reject empty/multiline values, preserve unrelated lines, deduplicate the updated key, and write atomically - not a general editor. Removal touches only the file, so process-env credentials can outlive removal. `.env` values never copy into `os.environ` and never log.
- Credential parsing and writing share the helpers in `core/utils/config.py`: only CR/LF delimit physical lines; other Unicode separators remain literal value characters. Values that would lose surrounding whitespace or matching quotes receive one outer quote pair; inner quotes and backslashes stay literal, without escape decoding or interpolation. The standalone reasoning probes use the same reader.

## Prompt fragments & blocks

Bundled fragments live in `resources/prompts/`; the seven editable block defaults are `runtime.md`, `identity_runtime.md`, `tools.md`, `tools_list.md`, `channels.md`, `skills.md`, `skill_maintenance.md`; backend-only one-shot briefs (`compaction.md`, `handoff.md`, `learn.md`, `reflect*.md`) are readable for internal Runs but never editable or copied to agent scopes. Nothing seeds default-scope copies anymore; a hand-created data-dir copy overrides bundled at read time.

Block persistence: each scope persists ordered `layout.json` plus thin text overrides under `blocks/<namespace>/<slug>.md` (default scope under `<data_dir>/prompts/`, agent scope under the agent's prompts dir). Block ids map to paths with **the colon never reaching disk** (Windows-safe); namespaces are a closed set; slugs validate with the agent-id rule before any path construction - `PromptBlockStore` is the single id-to-path writer raising on unsafe ids rather than sanitizing. A missing override reads as absent (cascade falls through); dynamic blocks have no override path. The override cascade composes in the prompts domain from these per-scope reads. `layout.json` is `{"format_version": 1, "entries": [...]}` (JSON Document Contract in `settings.md`; unknown fields survive writes). Missing layouts return `[]`; unreadable, invalid UTF-8/JSON, unversioned or other-version, or invalid-entry layouts warn and return `[]`, and `write_layout`/`prune_layout` refuse to overwrite them (`StorageError`) unless called with `reset=True` (the Prompts layout reset). Validation remains strict for entry types and unsafe scope ids; Prompts interprets an empty layout as no custom layout (`prompt_blocks.py`, `test_prompt_blocks.py`).

## Conventions

- Settings writes are UTF-8 sorted indented JSON with trailing newline. Atomic writes go through shared helpers staging under `artifacts/temp/atomic/` with fsync + `os.replace` (+ POSIX directory fsync).
- Runtime Settings/credential read-modify-write serializes per-process only - not cross-process locks. The installer is a separate boundary: its explicit-port update of an existing `settings.json` uses a cross-process sidecar lock plus same-directory atomic replace so parallel setup processes cannot lose unrelated changes or expose partial JSON.
- Fragment names and Agent IDs allowlist before path construction; traversal and absolute fragment paths are invalid storage data, never sanitized inputs.
- New code routing: section validation/normalization -> settings domain; fragment/block file access -> the two stores; categorized temp leases -> `TemporaryFileManager`; atomic staging -> `core/utils/atomic.py`. `StorageManager` composes and orchestrates - no new normalization or path logic there.

## Constraints & Gotchas

- Directory creation implies nothing about data ownership (see layout section) - domain formats and lifecycles stay with owning domains.
- Durable artifacts (attachments/speech/models/debug/performance/live-calls) are not temporary-file cleanup candidates; caller-owned generated images sit outside both.
- User-editable JSON validates before runtime consumption: Storage gates settings.json while other domains use validated loaders from the same module.
- Default fragments read bundled resources unless a hand-created data-dir copy exists - such stale copies shadow bundled updates and should be deleted.
- Agent-scope prompt seeding copies current effective content once when custom prompts activate; missing agent-scope fragments read as `""` and assemble only under the enabled flag. Block layout writes are inert-tolerant (pruning contributor-gone ids is normal, never an error) and seeding preserves existing layouts without copying text overrides.
- Backend-only briefs remain readable for internal Runs but are invisible to the System Prompt UI and never enter agent scopes.

Local speech owns managed SDK environments and setup-completion receipts under
`DataDirectoryLayout.speech_engines` (`<data-dir>/speech-engines/`), created only
by explicit speech setup. These are durable installation data, not temporary
artifacts; formats/lifecycle belong to `speech_setup.py` (`model_tasks/speech.md`).
