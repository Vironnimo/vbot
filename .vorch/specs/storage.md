# Storage

Data-directory setup, settings persistence, and bundled prompt fragment access.

## Overview

`core/storage/` owns Phase 2 data-directory creation and simple JSON/settings storage. It also mediates prompt fragment access so prompt bodies live in files rather than hardcoded code strings. The default data directory is `~/.vbot` unless supplied directly or through config.

## Data Model

Phase 2 creates these directories under `data_dir`: `.tmp`, `agents`, `archive`, `attachments`, `channels`, `cron`, `oauth`, `prompts`, `skills`, `logs`.

Bundled prompt fragments live in `resources/prompts/`: `system.md`, `runtime.md`, `tools.md`, `skills.md`.

`<data_dir>/.env` stores user-owned secrets such as provider API keys and acts
as a read-only fallback credential source.

`<data_dir>/settings.json` may include:

- `appearance.language` — persisted WebUI language preference.
- `skill_directories` — additional skill scan root paths configured from the Settings UI.
- `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` — integer limits for sub-agent tool execution.
- `attachment_max_size_bytes` — integer attachment upload limit for the runtime-owned `AttachmentStore`, default `20971520`.

## Interfaces

- `core/storage/__init__.py` exports `StorageManager`, `StorageError`, data-dir constants, and the storage config protocol.
- `StorageManager(data_dir=None, config=None, resources_dir=None)`
- `ensure_directories()` — creates the Phase 2 directory structure.
- `load_environment() -> dict[str, str]` — returns a read-only snapshot of
  credentials from `<data_dir>/.env` without mutating `os.environ`.
- `load_data_dir_credentials() -> dict[str, str]` — reads `<data_dir>/.env`
  as a fallback credential source.
- `build_environment_snapshot() -> dict[str, str]` — returns a merged
  process-env-over-data-dir credential snapshot without mutating process state.
- `load_settings() -> dict` — returns `{}` when `settings.json` does not exist.
- `save_settings(settings)` — atomically writes sorted, indented JSON.
- `load_appearance_settings() -> dict[str, str]` and `update_appearance_settings(appearance)` — read/write the supported Appearance settings subset.
- `load_skill_directory_settings() -> list[str]` and `update_skill_directory_settings(directories)` — read/write normalized extra skill scan directories.
- `load_subagent_settings() -> dict[str, int]` — reads supported sub-agent execution limits, defaulting to depth `4`, per-turn count `8`, and timeout `60` minutes.
- `copy_prompt_fragments(overwrite=False) -> list[Path]` — copies bundled prompt fragments into `<data_dir>/prompts/`.
- `read_prompt_fragment(fragment_name) -> str` — reads user copy first, then bundled resource fallback.

## Conventions

- Settings are UTF-8 JSON objects only.
- `.env` parsing is conservative: `KEY=VALUE`, blank/comment lines ignored,
  matching single/double quotes stripped, no expansion or command substitution.
- Do not log secret values from `.env`.
- `.env` values must not be copied back into `os.environ`; callers receive
  snapshots instead.
- Prompt fragment names are allowlisted; path traversal and absolute paths are rejected.
- User-edited prompt fragments are preserved unless `overwrite=True` is explicitly passed.
- Skill directory settings are stored as a list of non-empty absolute paths or home-relative paths beginning with `~`. Path existence is not validated during settings write; invalid or missing scan roots are ignored by skill loading.
- `attachment_max_size_bytes` is read as a plain integer from `settings.json`; invalid or missing values fall back to the runtime default.

## Constraints & Gotchas

- Atomic writes use temp files in `<data_dir>/.tmp/`; callers must ensure directories exist through `ensure_directories()` or methods that call it.
- `ensure_directories()` is the owner of the `<data_dir>/attachments/` root used by `AttachmentStore`; attachment code should not invent a parallel storage root.
