# Storage

Data-directory setup, settings persistence, and bundled prompt fragment access.

## Overview

`core/storage/` owns data-directory creation and simple JSON/settings storage. It also mediates raw prompt fragment file access so prompt bodies live in files rather than hardcoded code strings. The default data directory is `~/.vbot` unless supplied directly or through config. Public Settings update payload validation lives in `core/settings/`; storage validates and normalizes persisted subsets after that public schema has been accepted. System Prompt assembly and editable prompt-fragment rules live in `core/prompts/`.

## Data Model

Storage creates these directories under `data_dir`: `.tmp`, `agents`, `archive`, `attachments`, `channels`, `cron`, `oauth`, `prompts`, `recall`, `skills`, `logs`, `speech`.

Bundled prompt fragments live in `resources/prompts/`: `system.md`, `runtime.md`, `tools.md`, `channels.md`, `skills.md`, and the internal compaction prompt `compaction.md`.

Agent-scoped editable prompt fragments live under
`<data_dir>/agents/<agent-id>/prompts/`. Agent scopes may contain only the five
normal editable system-prompt fragments: `system.md`, `runtime.md`, `tools.md`,
`channels.md`, and `skills.md`. `compaction.md` is never Agent-scoped.

`<data_dir>/.env` stores user-owned secrets such as provider API keys and acts
as a read-only fallback credential source.

`<data_dir>/settings.json` may include:

- `appearance.language` — persisted WebUI language preference.
- `skill_directories` — additional skill scan root paths configured from the Settings UI.
- `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` — integer limits for sub-agent tool execution.
- `compaction` — automatic history-compaction settings `{ auto, threshold, tail_tokens, summary_model }`.
- `attachment_max_size_bytes` — integer attachment upload limit for the runtime-owned `AttachmentStore`, default `20971520`.
- `defaults.agent` — project-wide fallback Agent values `{ model?, fallback_model?, temperature?, thinking_effort? }`. Missing keys mean no global default for that field; `thinking_effort: ""` is a valid explicit "provider default" value.
- `recall.backend` — raw Session recall backend selection, default
  `jsonl_scan`; `sqlite_fts` stores a disposable derived index under
  `<data_dir>/recall/`.
- `model_tasks` — specialized task-model bindings keyed by supported task type,
  with one `target` string and one JSON-object `options` mapping per task.

## Interfaces

- `core/storage/__init__.py` exports `StorageManager`, `StorageError`, data-dir constants, and the storage config protocol.
- `StorageManager(data_dir=None, config=None, resources_dir=None)`
- `ensure_directories()` — creates the current data-directory structure.
- `load_environment() -> dict[str, str]` — returns a read-only snapshot of
  credentials from `<data_dir>/.env` without mutating `os.environ`.
- `load_data_dir_credentials() -> dict[str, str]` — reads `<data_dir>/.env`
  as a fallback credential source.
- `set_data_dir_credential(key, value)` — validates and atomically writes or
  replaces one single-line credential in `<data_dir>/.env` through a temp file
  under `<data_dir>/.tmp/` plus `os.replace()`.
- `build_environment_snapshot() -> dict[str, str]` — returns a merged
  process-env-over-data-dir credential snapshot without mutating process state.
- `load_settings() -> dict` — returns `{}` when `settings.json` does not exist.
- `save_settings(settings)` — atomically writes sorted, indented JSON.
- `load_appearance_settings() -> dict[str, str]` and `update_appearance_settings(appearance)` — read/write the supported Appearance settings subset.
- `load_skill_directory_settings() -> list[str]` and `update_skill_directory_settings(directories)` — read/write normalized extra skill scan directories.
- `load_subagent_settings() -> dict[str, int]` — reads supported sub-agent execution limits, defaulting to depth `4`, per-turn count `8`, and timeout `60` minutes.
- `load_compaction_settings() -> dict[str, Any]` / `update_compaction_settings(compaction)` — read/write normalized compaction settings. `threshold` must be numeric in `(0, 1]`, `tail_tokens` must be a positive integer, and `summary_model` is `str | None`.
- `load_defaults() -> dict[str, Any]` / `update_defaults(section, values) -> dict[str, Any]` — read/write validated `settings.json` defaults blocks. Currently only `section="agent"` is supported.
- `load_recall_settings() -> dict[str, str]` / `update_recall_settings(recall)` — read/write normalized recall backend settings, defaulting to `{"backend": "jsonl_scan"}`.
- `load_model_task_settings() -> dict[str, dict[str, Any]]` /
  `update_model_task_settings(model_tasks)` — read/write normalized sparse
  task-model bindings. Empty target strings remove that task binding.
- `copy_prompt_fragments(overwrite=False) -> list[Path]` — copies bundled prompt fragments into `<data_dir>/prompts/`.
- `read_prompt_fragment(fragment_name) -> str` — reads user copy first, then bundled resource fallback.
- `copy_agent_prompt_fragments(agent_id, overwrite=False) -> list[Path]` —
  copies the current effective default-scope editable fragments into one
  Agent's prompt directory, preserving existing Agent files unless `overwrite`
  is true.
- `agent_prompts_dir(agent_id) -> Path` — returns
  `<data_dir>/agents/<agent-id>/prompts/` after validating the Agent id.
- `agent_prompt_fragment_exists(agent_id, fragment_name) -> bool` — reports
  whether one Agent-scoped fragment exists on disk.
- `read_agent_prompt_fragment(agent_id, fragment_name) -> str` — reads an
  Agent-scoped fragment, returning `""` when the file is missing.
- `write_agent_prompt_fragment(agent_id, fragment_name, content) -> Path` —
  writes one Agent-scoped fragment atomically.
- `reset_agent_prompt_fragment(agent_id, fragment_name) -> Path` — copies the
  current effective default-scope content for that fragment into the Agent
  scope.

## Conventions

- Settings are UTF-8 JSON objects only.
- `.env` parsing is conservative: `KEY=VALUE`, blank/comment lines ignored,
  matching single/double quotes stripped, no expansion or command substitution.
- Do not log secret values from `.env`.
- `.env` values must not be copied back into `os.environ`; callers receive
  snapshots instead.
- Prompt fragment names are allowlisted; path traversal and absolute paths are rejected.
- Prompt Agent IDs are validated before building Agent prompt paths. Prompt
  fragment names are allowlisted separately for default and Agent scopes; path
  traversal and absolute paths are rejected.
- `compaction.md` is allowlisted for backend prompt loading but is not part of the normal system-prompt editor/viewer surface, and it is never copied into Agent prompt scopes.
- User-edited prompt fragments are preserved unless `overwrite=True` is explicitly passed.
- Skill directory settings are stored as a list of non-empty absolute paths or home-relative paths beginning with `~`. Path existence is not validated during settings write; invalid or missing scan roots are ignored by skill loading.
- `attachment_max_size_bytes` is read as a plain integer from `settings.json`; invalid or missing values fall back to the runtime default.
- `update_defaults("agent", ...)` validates only the supported four Agent-default fields, removes individual keys when a value is `null`, bounds `temperature` to `[0, 2]`, and allows `thinking_effort` to be either `null`, `""`, or one of the normal effort tokens.
- `update_model_task_settings(...)` validates task keys through
  `core/model_tasks/`, persists only non-empty targets, preserves JSON-object
  options, and removes the top-level `model_tasks` key when no bindings remain.

## Constraints & Gotchas

- Atomic writes use temp files in `<data_dir>/.tmp/`; callers must ensure directories exist through `ensure_directories()` or methods that call it.
- `ensure_directories()` is the owner of the `<data_dir>/attachments/` root used by `AttachmentStore`; attachment code should not invent a parallel storage root.
- `ensure_directories()` also creates `<data_dir>/speech/`, but speech artifact
  metadata and binary writes are owned by `core/speech/`.
