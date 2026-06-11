# Settings

Public settings schema parsing and central JSON validation for user-editable runtime configuration files.

## Overview

`core/settings/` owns the product-facing schema for Settings update payloads. It validates what public accessors may send through `settings.update` before the server applies those updates to storage and runtime side effects. It also owns a central raw-file validator in `core/settings/validation.py` for user-editable runtime JSON: `settings.json`, `agents/*/agent.json`, `channels/*/channel.json`, and `cron/jobs.json`. Local doctor checks and runtime read paths use the same validators so manual edit errors fail fast with consistent diagnostics. Raw `settings.json` keys and public `settings.update` sections overlap, but they are not the same API; keep that distinction explicit when adding settings.

Storage still owns raw `settings.json` file I/O, process-local locked read-modify-write transactions, atomic writes, prompt fragments, and normalized persistence helpers. Server delegates own RPC error mapping and side effects such as reloading skills after skill directory changes. The raw `settings.set_key` RPC validates the merged raw `settings.json` mapping with `validate_settings_data()` before persisting; warnings such as unknown top-level keys are allowed, but schema errors for known sections are rejected as RPC `invalid_request`.

## Where Settings Code Lives

When adding or changing a setting, touch the layer that owns it:

- Raw `settings.json` validation → `core/settings/validation.py` (`KNOWN_RAW_SETTINGS_KEYS` plus a `_validate_*` helper).
- Public `settings.update` schema → `core/settings/settings.py` (`SETTINGS_UPDATE_SECTIONS` plus a `_parse_*` helper).
- Section normalization and defaults → `core/settings/normalizers.py` (stateless `normalize_*` / `coerce_*` per section; raises `StorageError`).
- Persistence (transactions, atomic write, `load_*` / `update_*` accessors) → `core/storage/storage.py`, which delegates all section knowledge to the normalizers.
- Live runtime side effect → a reload hook in `server/rpc/settings_methods._update_settings`. Today only `skills` → `runtime.reload_skills()` and `recall` → `runtime.reload_recall_backend()` reload live; every other section takes effect on the next start.

## Interfaces

- `core/settings/__init__.py` exports `parse_settings_update`, `SettingsValidationError`, central JSON validator report types, load helpers, file validators, data validators, and shared Settings schema constants (including `DEFAULT_APPEARANCE_LANGUAGE`, `SUPPORTED_APPEARANCE_LANGUAGES`, `DEFAULT_RECALL_SETTINGS`).
- `core/settings/normalizers.py` owns stateless per-section normalization for persisted `settings.json` sections (appearance, skills, sub-agents, compaction, defaults/agent, recall, debug, web search, model tasks) plus the section default constants. Functions raise `StorageError` (defined in `core/utils/errors.py`) on invalid persisted data; `StorageManager` calls them inside its transactions. The settings domain must not import `core/storage/` — that direction would recreate the import cycle the error relocation removed.
- `parse_settings_update(params) -> dict[str, Any]` validates the public `settings.update` request body and returns a normalized per-section update dict.
- `SettingsValidationError` signals malformed public payloads. Server delegates map it to RPC `invalid_request`.
- `validate_settings_file(path) -> SettingsValidationReport` validates one raw `settings.json` file without writing or normalizing it. Missing files are OK because storage defaults apply.
- `validate_agent_file(path)`, `validate_channel_file(path)`, and `validate_cron_jobs_file(path)` validate persisted Agent, Channel, and Cron JSON files without loading runtime services.
- `validate_data_dir_config(data_dir) -> tuple[JsonValidationReport, ...]` validates the current user-editable config bundle for `vbot doctor config`.
- `load_validated_settings_json`, `load_validated_agent_json`, `load_validated_channel_json`, and `load_validated_cron_jobs_json` are the read-time gates used by storage/runtime domains before consuming raw JSON.
- Data validators return diagnostics with severity, JSON path, and message.

## Raw Settings Keys

`core/settings/validation.py` is the source of truth for raw top-level keys accepted in `<data_dir>/settings.json`: port aliases (`PORT`, `SERVER_PORT`, `port`, `server_port`), `appearance`, `skill_directories`, `extension_directories`, upload limits (`attachment_max_size_bytes`, `speech_upload_max_size_bytes`), sub-agent limits (`max_subagent_depth`, `max_subagents_per_turn`, `subagent_timeout_minutes`), `compaction`, `defaults`, `recall`, `web_search`, `model_tasks`, and `debug`. Unknown top-level raw keys are warnings, not errors, but schema errors for known sections are fatal before runtime code consumes them.

Raw-only settings are not public `settings.update` sections. `extension_directories` is loaded only during `Runtime.start()` as extra roots for Python extension discovery; there is no public Settings UI/RPC section that reloads extensions live. `attachment_max_size_bytes` is read at runtime startup for `AttachmentStore`, and `speech_upload_max_size_bytes` is read at runtime startup for the server speech upload gate. Port aliases are consumed by server startup, not by the settings update parser.

## Supported Update Sections

- `appearance` — `{ language: string }`; language must be non-empty. Storage validates the supported language set.
- `skills` — `{ directories: string[] }`; storage validates and normalizes absolute or home-relative paths. Both POSIX and Windows absolute forms are accepted on any host, so a settings.json validates identically across platforms.
- `defaults` — `{ agent: { model?, fallback_model?, temperature?, thinking_effort? } }`; `null` removes an individual Agent default. `temperature` is bounded `0.0`–`2.0`. `thinking_effort: ""` is a valid explicit provider-default value.
- `subagents` — requires `max_subagent_depth`, `max_subagents_per_turn`, and `subagent_timeout_minutes` as positive integers.
- `compaction` — requires `{ auto, threshold, tail_tokens, summary_model }`; `threshold` must be numeric in `(0, 1]`, `tail_tokens` must be a positive integer, and `summary_model` is `str | null`.
- `recall` — `{ backend: "jsonl_scan" | "sqlite_fts" }`; updates the backend used by the `session_search` tool.
- `web_search` — `{ provider: "brave" | "searxng", searxng?: { base_url: string } }`; updates the provider used by the `web_search` tool. `provider` is required in public updates; `searxng.base_url` defaults to `http://localhost:8888` when not persisted.
- `debug` — `{ enabled?: boolean, trace_limit?: positive integer }`; both fields are optional in public updates. `enabled` defaults to `false`, `trace_limit` defaults to `50` and is rejected above `500` (not silently clamped). Updates merge with existing settings — partial updates preserve unspecified fields.
- `model_tasks` — `{ <task_type>: { target?, options? } }`; supported task types are owned by `core/model_tasks/`. `target` must be a string when present, `options` must be an object, and an empty target clears that task's persisted binding.

## Constraints & Gotchas

- Public schema errors must remain transport-independent in `core/settings/`; do not raise server RPC errors from this module.
- Raw JSON diagnostics must remain transport-independent and file-system local. Formatting for agents belongs to CLI doctor commands.
- Unknown top-level raw settings keys are warnings, not errors, because raw settings may temporarily contain values not consumed by current runtime code.
- Raw `settings.json` and public `settings.update` use different shapes for the same data: raw keys are flat (`skill_directories`, `max_subagent_depth` / `max_subagents_per_turn` / `subagent_timeout_minutes`) while public sections nest them (`skills.directories`, `subagents.{…}`). `appearance`, `defaults`, `compaction`, `recall`, `web_search`, `model_tasks`, and `debug` keep the same name on both surfaces. Use the shape that matches whichever surface you touch.
- Raw section validators (`recall`, `web_search`, `model_tasks`, `compaction`, `debug`, `appearance`, `defaults`) are nullable: an absent or `null` section is fine, but a present one must be an object and is checked per field. `recall.backend` only has to be lowercase snake_case at the raw layer — runtime resolves it through `RecallBackendRegistry` and falls back to `jsonl_scan` for unknown names — whereas the public `settings.update` parser is stricter and rejects any backend outside `jsonl_scan`/`sqlite_fts`.
- `settings.py` stays focused on public `settings.update` parsing. Raw file validation belongs in `validation.py`.
- `settings.update` accepts sparse Defaults and Model Task updates but full Sub-Agent and Compaction sections. Recall updates are small exact-section writes with only `backend`.
- Runtime side effects do not live here. For example, saving skill directories still happens in storage, and server delegates call `runtime.reload_skills()` after a successful update. Recall backend changes are applied by server delegates through `runtime.reload_recall_backend()`.
- Accepted multi-section `settings.update` payloads are persisted by storage in one locked settings transaction before server-side reload hooks run.
- `settings.web_search` is loaded by the `web_search` tool at call time, so changing the selected search provider does not require a runtime restart or tool re-registration.
- Keep storage-level validation errors distinct from public schema errors so RPC error mapping remains stable.
