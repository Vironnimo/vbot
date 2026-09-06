# Storage

Data-directory bootstrap, temporary-file lifecycle, atomic settings and credential persistence, and raw prompt-fragment file access.

## Overview

`core/storage/` owns the process-local filesystem services and the canonical placement contract for the runtime data root. It resolves the data directory (explicit value -> `DATA_DIR`/`VBOT_DATA_DIR` -> config -> checkout markers -> `~/.vbot`; a `cwd_only` marker applies only in its own step, letting a source checkout carry a dev data dir without redirecting installed CLIs), initializes the complete canonical structure, manages categorized temporary files, and reads/writes `settings.json`, the data-dir `.env`, prompt fragments, and System Prompt block persistence. Storage owns placement but not schemas (`core/settings/`), prompt assembly rules (`core/prompts/`), or domain records stored in those directories; generated images are caller-owned Workspace/Project files outside the layout.

## Canonical layout

`DataDirectoryLayout` is the immutable path contract: `artifacts/{attachments,speech,models,debug,temp/{atomic,bash,subagents,terminals}}`, `statistics/` (Provider-owned usage history plus the Statistics-owned disposable SQLite), and independent roots `agents`, `archive`, `bootstrap`, `channels`, `cron`, `extensions`, `logs`, `oauth`, `processes`, `projects`, `prompts`, `recall`, `skills`, `terminals`. The canonical Session files `<data-dir>/sessions.db` and `<data-dir>/session-store.json` are owned by `core/sessions/`; verified snapshots, quarantine bundles, and recovery incidents have separate fixed roots there. Initialization creates the full set non-destructively - `.env.example` copies only when `.env` is absent, empty settings only when missing; creation failures in the canonical directory are fatal.

Directory creation does not transfer ownership: Agents own their trees, Channels theirs, Cron/Bootstrap their job stores, Attachments/Speech their artifacts, Models the Model DB, Providers `statistics/provider-usage/`, Statistics its read model, Terminal Manager launch history. Layout changes belong in `layout.py`; format/retention changes stay with owning domains.

Browser Use creates its own durable native-client cache under `artifacts/browser-use/`; it is not a `TemporaryFileManager` retention category or a Storage bootstrap directory. Its shared Chrome for Testing cache is outside the data directory at `~/.agent-browser/browsers`. Preparation and locking belong to `resources/extensions/browser_use/runtime.py`; see `extensions/browser-use.md`.

## Temporary files

`TemporaryFileManager` owns only retained categories under `artifacts/temp/`: `bash`, `subagents`, `terminals`. Leases protect active files; idempotent completion starts retention (72 h bash/terminals, 24 h subagents); cleanup runs at start and periodically, removing only expired inactive regular files. The `atomic` child is short-lived staging removed by producers, not a retention category. This lifecycle is internal - Extensions get no temporary-file API.

## Settings & credentials

- `<data_dir>/settings.json`: raw I/O, validation-gated loading, normalized persistence helpers, locked transactions - schema owned by `core/settings/`.
- `<data_dir>/.env` is a user-owned credential fallback: snapshot reads plus single-key updates; process environment keeps higher precedence when callers merge. Read failures log and yield an empty snapshot rather than blocking startup.
- Credential writes validate shell-style keys, reject empty/multiline values, preserve unrelated lines, deduplicate the updated key, and write atomically - not a general editor. Removal touches only the file, so process-env credentials can outlive removal. `.env` values never copy into `os.environ` and never log.

## Prompt fragments & blocks

Bundled fragments live in `resources/prompts/`; the seven editable block defaults are `runtime.md`, `identity_runtime.md`, `tools.md`, `tools_list.md`, `channels.md`, `skills.md`, `skill_maintenance.md`; backend-only one-shot briefs (`compaction.md`, `handoff.md`, `learn.md`, `reflect*.md`) are readable for internal Runs but never editable or copied to agent scopes. Nothing seeds default-scope copies anymore; a hand-created data-dir copy overrides bundled at read time.

Block persistence: each scope persists ordered `layout.json` plus thin text overrides under `blocks/<namespace>/<slug>.md` (default scope under `<data_dir>/prompts/`, agent scope under the agent's prompts dir). Block ids map to paths with **the colon never reaching disk** (Windows-safe); namespaces are a closed set; slugs validate with the agent-id rule before any path construction - `PromptBlockStore` is the single id-to-path writer raising on unsafe ids rather than sanitizing. A missing override reads as absent (cascade falls through); dynamic blocks have no override path. The override cascade composes in the prompts domain from these per-scope reads.

## Conventions

- Settings writes are UTF-8 sorted indented JSON with trailing newline. Atomic writes go through shared helpers staging under `artifacts/temp/atomic/` with fsync + `os.replace` (+ POSIX directory fsync).
- Runtime Settings/credential read-modify-write serializes per-process only - not cross-process locks. The installer is a separate boundary: its explicit-port update of an existing `settings.json` uses a cross-process sidecar lock plus same-directory atomic replace so parallel setup processes cannot lose unrelated changes or expose partial JSON.
- Fragment names and Agent IDs allowlist before path construction; traversal and absolute fragment paths are invalid storage data, never sanitized inputs.
- New code routing: section validation/normalization -> settings domain; fragment/block file access -> the two stores; categorized temp leases -> `TemporaryFileManager`; atomic staging -> `core/utils/atomic.py`. `StorageManager` composes and orchestrates - no new normalization or path logic there.

## Constraints & Gotchas

- Directory creation implies nothing about data ownership (see layout section) - domain formats and lifecycles stay with owning domains.
- Durable artifacts (attachments/speech/models/debug) are not temporary-file cleanup candidates; caller-owned generated images sit outside both.
- User-editable JSON validates before runtime consumption: Storage gates settings.json while other domains use validated loaders from the same module.
- Default fragments read bundled resources unless a hand-created data-dir copy exists - such stale copies shadow bundled updates and should be deleted.
- Agent-scope prompt seeding copies current effective content once when custom prompts activate; missing agent-scope fragments read as `""` and assemble only under the enabled flag. Block layout writes are inert-tolerant (pruning contributor-gone ids is normal, never an error) and seeding preserves existing layouts without copying text overrides.
- Backend-only briefs remain readable for internal Runs but are invisible to the System Prompt UI and never enter agent scopes.
