# Storage

Data-directory setup, settings persistence, and bundled prompt fragment access.

## Overview

`core/storage/` owns Phase 2 data-directory creation and simple JSON/settings storage. It also mediates prompt fragment access so prompt bodies live in files rather than hardcoded code strings. The default data directory is `~/.vbot` unless supplied directly or through config.

## Data Model

Phase 2 creates these directories under `data_dir`: `.tmp`, `agents`, `archive`, `channels`, `cron`, `oauth`, `prompts`, `skills`, `logs`.

Bundled prompt fragments live in `resources/prompts/`: `system.md`, `runtime.md`, `tools.md`, `skills.md`.

`<data_dir>/.env` stores user-owned secrets such as provider API keys.

## Interfaces

- `core/storage/__init__.py` exports `StorageManager`, `StorageError`, data-dir constants, and the storage config protocol.
- `StorageManager(data_dir=None, config=None, resources_dir=None)`
- `ensure_directories()` — creates the Phase 2 directory structure.
- `load_environment()` — loads `<data_dir>/.env` into `os.environ` without
  overwriting existing process environment variables.
- `load_settings() -> dict` — returns `{}` when `settings.json` does not exist.
- `save_settings(settings)` — atomically writes sorted, indented JSON.
- `copy_prompt_fragments(overwrite=False) -> list[Path]` — copies bundled prompt fragments into `<data_dir>/prompts/`.
- `read_prompt_fragment(fragment_name) -> str` — reads user copy first, then bundled resource fallback.

## Conventions

- Settings are UTF-8 JSON objects only.
- `.env` parsing is conservative: `KEY=VALUE`, blank/comment lines ignored,
  matching single/double quotes stripped, no expansion or command substitution.
- Do not log secret values from `.env`.
- Prompt fragment names are allowlisted; path traversal and absolute paths are rejected.
- User-edited prompt fragments are preserved unless `overwrite=True` is explicitly passed.

## Constraints & Gotchas

- Atomic writes use temp files in `<data_dir>/.tmp/`; callers must ensure directories exist through `ensure_directories()` or methods that call it.
