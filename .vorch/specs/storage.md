# Storage

Data-directory bootstrap, atomic settings and credential persistence, and raw prompt-fragment file access.

## Overview

`core/storage/` owns the process-local filesystem services for the runtime data root. It resolves the data directory from an explicit constructor value, config `data_dir`, `DATA_DIR` / `VBOT_DATA_DIR`, or the default `~/.vbot`, creates the shared bootstrap directory skeleton, and reads/writes `settings.json`, the data-dir `.env`, and prompt fragment files. It does not own the product-facing Settings schema (`core/settings/`), prompt assembly/edit rules (`core/prompts/`), or domain records such as Agents, Channels, Cron jobs, Attachments, Speech artifacts, and Image artifacts even when those records live under directories created by Storage. Runtime wires one `StorageManager` and passes its `data_dir` to the domain stores that own their own file formats.

## Data Model

`ensure_directories()` creates the shared bootstrap directories under `<data_dir>`: `.tmp`, `agents`, `archive`, `attachments`, `channels`, `cron`, `debug`, `oauth`, `prompts`, `recall`, `speech`, `skills`, and `logs`.

`<data_dir>/settings.json` is a UTF-8 JSON object. Storage owns raw file I/O, validation-gated loading, normalized persistence helpers, and locked read-modify-write transactions; `.vorch/specs/settings.md` owns the raw key list and public `settings.update` section shapes.

`<data_dir>/.env` is a user-owned credential fallback file. Storage can read it as a snapshot or update one single-line credential key; process environment values remain higher precedence when callers request a merged environment snapshot.

Bundled prompt fragments live in `resources/prompts/`. Default-scope user copies live in `<data_dir>/prompts/` and may include `system.md`, `runtime.md`, `tools.md`, `channels.md`, `skills.md`, and backend-only `compaction.md`. Agent-scoped prompt fragments live in `<data_dir>/agents/<agent-id>/prompts/` and may contain only the five normal editable fragments: `system.md`, `runtime.md`, `tools.md`, `channels.md`, and `skills.md`.

Other domains may create additional data under the same root on demand. For example, `core/image/` owns `<data_dir>/images/`, `core/debug/` owns trace files under `<data_dir>/debug/`, and `core/automation/` owns extra Cron internals under `<data_dir>/cron/`. Do not add a directory to Storage's bootstrap list just because a domain can create it itself.

## Interfaces

- `core/storage/__init__.py` exports `StorageManager`, `StorageError`, `ConfigProtocol`, `DEFAULT_DATA_DIR`, `PHASE_TWO_DIRECTORIES`, prompt constants, and appearance/recall defaults used by callers and tests. Import from the package; the internal module split is not a public surface.
- `StorageManager(data_dir=None, config=None, resources_dir=None)` resolves `data_dir`, stores the resources root, and owns a process-local re-entrant lock for settings transactions.
- Data-root and credentials: `ensure_directories()`, `load_environment()`, `load_data_dir_credentials()`, `set_data_dir_credential(key, value)`, and `build_environment_snapshot()`.
- Raw settings transactions: `load_settings()`, `save_settings(settings)`, `update_settings(mutator)`, and `update_settings_sections(settings_update)`. `load_settings()` returns `{}` for a missing file and raises `StorageError` for invalid JSON/schema diagnostics from `core/settings/validation.py`.
- Normalized settings helpers cover Appearance, Skills directories, Sub-Agent limits, Compaction, Agent defaults, Recall, Web Search, Debug, and Model Task bindings. Keep shape details in `.vorch/specs/settings.md`; Storage's job is to normalize, merge, delete empty sections where applicable, and persist.
- Prompt fragments: `copy_prompt_fragments()`, `read_prompt_fragment()`, `write_prompt_fragment()`, `reset_prompt_fragment()`, plus Agent-scope helpers `copy_agent_prompt_fragments()`, `agent_prompts_dir()`, `agent_prompt_fragment_exists()`, `read_agent_prompt_fragment()`, `write_agent_prompt_fragment()`, and `reset_agent_prompt_fragment()`.

## Conventions

- Settings writes are UTF-8, sorted, indented JSON with a trailing newline.
- Settings read-modify-write helpers are serialized only inside the current process. They are not cross-process locks.
- Atomic Storage writes use temp files under `<data_dir>/.tmp/` plus `os.replace()`. Callers must ensure the data directory skeleton exists or use Storage methods that call `ensure_directories()`.
- `.env` parsing is conservative: `KEY=VALUE`, blank/comment lines ignored, matching quotes stripped, no expansion or command substitution.
- `.env` values must never be copied back into `os.environ` and must never be logged.
- Prompt fragment names and Agent IDs are allowlisted before paths are constructed. Path traversal and absolute fragment paths are invalid storage data, not inputs to sanitize later.
- `update_settings_sections()` expects a parsed public Settings update from `core/settings.parse_settings_update()`. If any section fails Storage-level normalization, the existing `settings.json` is left unchanged.
- Where new code goes: stateless per-section validation/normalization belongs in `settings_normalizers.py`, prompt-fragment file access in `prompt_fragments.py` (`PromptFragmentStore`, owned and delegated to by `StorageManager`), and shared temp-file/atomic writes in `atomic.py`. `StorageManager` stays the orchestration entry point, not a home for new normalization or path logic.

## Constraints & Gotchas

- Directory creation does not imply data ownership. `AgentStore` owns `agents/<id>/agent.json` and workspaces, `ChannelService` owns `channels/<id>/channel.json`, `CronService` owns `cron/jobs.json`, `AttachmentStore` owns attachment blobs and sidecars, `SpeechService` owns speech artifacts, and `ImageService` owns image artifacts.
- User-editable JSON is validated before runtime consumption through `core/settings/validation.py`: Storage gates `settings.json`, while Agent, Channel, and Cron domains use their own validated loaders from the same module.
- `update_settings_sections()` validates and merges all accepted sections in memory, writes once, and returns the updated sections. Server RPC delegates run live reload hooks only after that write succeeds; today Skills and Recall reload live, Web Search is read by the tool at call time, and most other settings take effect on next startup.
- `set_data_dir_credential()` validates shell-style environment keys, rejects empty or multiline values, preserves unrelated lines, removes duplicate occurrences of the updated key, and writes atomically. It is not a general `.env` editor.
- Default prompt fragments fall back to bundled resources when no user copy exists. `copy_prompt_fragments()` preserves user edits unless `overwrite=True`; `reset_prompt_fragment()` intentionally overwrites the default-scope user copy with the bundled resource.
- Agent prompt seeding and reset copy the current effective default-scope content, not necessarily the bundled resource. Missing Agent-scope fragments read as `""`; Agent prompt files are ignored by prompt assembly unless that Agent has `custom_system_prompt_enabled: true`.
- `compaction.md` is readable through Storage for backend compaction, but it is not editable through the System Prompt UI and is never copied into Agent prompt scopes.
