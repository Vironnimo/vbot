# Settings

Public settings schema parsing and validation for configuration updates.

## Overview

`core/settings/` owns the product-facing schema for Settings update payloads. It validates what public accessors may send through `settings.update` before the server applies those updates to storage and runtime side effects.

Storage still owns raw `settings.json` file I/O, atomic writes, prompt fragments, and normalized persistence helpers. Server delegates own RPC error mapping and side effects such as reloading skills after skill directory changes.

## Interfaces

- `core/settings/__init__.py` exports `parse_settings_update`, `SettingsValidationError`, and shared Settings schema constants.
- `parse_settings_update(params) -> dict[str, Any]` validates the public `settings.update` request body and returns a normalized per-section update dict.
- `SettingsValidationError` signals malformed public payloads. Server delegates map it to RPC `invalid_request`.

## Supported Update Sections

- `appearance` — `{ language: string }`; language must be non-empty. Storage validates the supported language set.
- `skills` — `{ directories: string[] }`; storage validates and normalizes absolute or home-relative paths.
- `defaults` — `{ agent: { model?, fallback_model?, temperature?, thinking_effort? } }`; `null` removes an individual Agent default. `thinking_effort: ""` is a valid explicit provider-default value.
- `subagents` — requires `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` as positive integers.
- `compaction` — requires `{ auto, threshold, tail_tokens, summary_model }`; `threshold` must be numeric in `(0, 1]`, `tail_tokens` must be a positive integer, and `summary_model` is `str | null`.

## Constraints & Gotchas

- Public schema errors must remain transport-independent in `core/settings/`; do not raise server RPC errors from this module.
- `settings.update` accepts sparse Defaults updates but full Sub-Agent and Compaction sections.
- Runtime side effects do not live here. For example, saving skill directories still happens in storage, and server delegates call `runtime.reload_skills()` after a successful update.
- Keep storage-level validation errors distinct from public schema errors so RPC error mapping remains stable.