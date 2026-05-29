# Settings

Public settings schema parsing and central JSON validation for user-editable
runtime configuration files.

## Overview

`core/settings/` owns the product-facing schema for Settings update payloads. It
validates what public accessors may send through `settings.update` before the
server applies those updates to storage and runtime side effects. It also owns a
central raw-file validator in `core/settings/validation.py` for user-editable
runtime JSON: `settings.json`, `agents/*/agent.json`,
`channels/*/channel.json`, and `cron/jobs.json`. Local doctor checks and
runtime read paths use the same validators so manual edit errors fail fast with
consistent diagnostics.
Raw `settings.json` also accepts `recall.backend` for backend selection, and
the public `settings.update` RPC accepts the first-party recall backend names
used by the Settings UI.

Storage still owns raw `settings.json` file I/O, atomic writes, prompt fragments, and normalized persistence helpers. Server delegates own RPC error mapping and side effects such as reloading skills after skill directory changes.

## Interfaces

- `core/settings/__init__.py` exports `parse_settings_update`,
	`SettingsValidationError`, central JSON validator report types, load helpers,
	file validators, data validators, and shared Settings schema constants.
- `parse_settings_update(params) -> dict[str, Any]` validates the public `settings.update` request body and returns a normalized per-section update dict.
- `SettingsValidationError` signals malformed public payloads. Server delegates map it to RPC `invalid_request`.
- `validate_settings_file(path) -> SettingsValidationReport` validates one raw
	`settings.json` file without writing or normalizing it. Missing files are OK
	because storage defaults apply.
- `validate_agent_file(path)`, `validate_channel_file(path)`, and
	`validate_cron_jobs_file(path)` validate persisted Agent, Channel, and Cron
	JSON files without loading runtime services.
- `validate_data_dir_config(data_dir) -> tuple[JsonValidationReport, ...]`
	validates the current user-editable config bundle for `vbot doctor config`.
- `load_validated_settings_json`, `load_validated_agent_json`,
	`load_validated_channel_json`, and `load_validated_cron_jobs_json` are the
	read-time gates used by storage/runtime domains before consuming raw JSON.
- Data validators return diagnostics with severity, JSON path, and message.

## Supported Update Sections

- `appearance` — `{ language: string }`; language must be non-empty. Storage validates the supported language set.
- `skills` — `{ directories: string[] }`; storage validates and normalizes absolute or home-relative paths.
- `defaults` — `{ agent: { model?, fallback_model?, temperature?, thinking_effort? } }`; `null` removes an individual Agent default. `thinking_effort: ""` is a valid explicit provider-default value.
- `subagents` — requires `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` as positive integers.
- `compaction` — requires `{ auto, threshold, tail_tokens, summary_model }`; `threshold` must be numeric in `(0, 1]`, `tail_tokens` must be a positive integer, and `summary_model` is `str | null`.
- `recall` — `{ backend: "jsonl_scan" | "sqlite_fts" }`; updates the backend used by the `session_search` tool.

## Constraints & Gotchas

- Public schema errors must remain transport-independent in `core/settings/`; do not raise server RPC errors from this module.
- Raw JSON diagnostics must remain transport-independent and file-system local.
  Formatting for agents belongs to CLI doctor commands.
- Unknown top-level raw settings keys are warnings, not errors, because raw
	settings may temporarily contain values not consumed by current runtime code.
- `settings.recall` must be an object when present. `settings.recall.backend`
  must be a non-empty lowercase snake_case string; runtime resolves it against
  the `RecallBackendRegistry` and falls back to `jsonl_scan` for unknown names.
- `settings.py` stays focused on public `settings.update` parsing. Raw file
  validation belongs in `validation.py`.
- `settings.update` accepts sparse Defaults updates but full Sub-Agent and Compaction sections. Recall updates are small exact-section writes with only `backend`.
- Runtime side effects do not live here. For example, saving skill directories still happens in storage, and server delegates call `runtime.reload_skills()` after a successful update. Recall backend changes are applied by server delegates through `runtime.reload_recall_backend()`.
- Keep storage-level validation errors distinct from public schema errors so RPC error mapping remains stable.
