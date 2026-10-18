# Settings sections

The contracts of individual Settings sections and paths: per-section update semantics and refusals, live effects per section, Task Model path rules, patch pruning of emptied containers, the `base` plumbing, Sub-Agent limits, and the installer's `defaults.agent` seed.

## Live effects per section

Live side effects come either from read-at-use accessors or from the shared Runtime Settings coordinator (`core/runtime/_settings.py`, `runtime.md`) used by both mutation surfaces: OpenRouter routing snapshots per Run, Skills directories reload, Recall reloads backend, Extension directories rebuild, disabled-set applies surgically, `keep_awake` toggles power request, application `timezone` updates Calendar and Cron, a changed `archive` section wakes the archive's retention sweep.

## Task Model paths

Changing a Task Model target leaf resets prior options before explicit option edits; unsetting that target removes the complete binding and overlaps every option edit for that task. Unchanged effective Task Model bindings skip live catalog validation.

## Patch pruning

`StorageManager.patch_settings()` takes a strict raw document snapshot under the Settings lock, then gives the path patcher and optional runtime validator only the known Settings. The raw snapshot is used only to decide whether ancestors emptied by a leaf reset still own unknown fields: those containers remain for the writer to merge, while genuinely empty ancestors and empty opaque data are pruned. Explicit whole-object removal (including a Task Model target reset, which removes its binding) also removes that object's unknown fields; unknown siblings remain. Unsetting a missing field is a no-op. Section-update defaults and Task Model paths likewise retain containers for unknown-sibling preservation.

## Update sections

Update sections are sparse or full per their semantics; notable contracts:

- `appearance.language` is required so partial updates never reset display prefs.
- `extensions` is a **full-replace** write whose disabled-delta routes enable through the full rebuild and disable through surgical deactivation, with config values applying live via each extension's reader.
- `providers` updates exactly OpenRouter routing (Custom Providers have dedicated RPCs coordinating registry reload and write-only credentials); OpenRouter routing's full schema lives in `providers/openrouter.md`.
- `recall.backend` defaults to `sqlite_fts` when absent and validates shape only here while membership resolves against the selectable `sqlite_fts`, `vector`, `hybrid` plus Extension backends at the server edge; canonical scan is only an internal fallback.
- `compaction.trigger` accepts either a pure `input_tokens` limit or a `context_ratio` with an optional `tokens` cap, where ratio and cap combine rather than replace one another.
- `local_models.context_windows` sparse-merges per key (`null` removes).
- `model_tasks` validates bindings against loaded Models/targets before persistence so generic mutation cannot bypass task validation.
- `session_titles` reads live per first message; `session_titles.model` is a Model binding that `settings.update` checks against the loaded Models and their allowed Connections before persisting (`_validate_model_connections`, `server/rpc/settings_methods.py`).
- `librarian` holds `enabled`, `interval_days`, `archive_after_days` and `consolidate`, and the Librarian's Model is the built-in Librarian Agent's own (`agent.md`): an earlier vBot's `librarian.model` loads without error and is dropped by the next `librarian` write, `settings.update` refuses it (`unsupported librarian settings: model`), and `doctor config` warns `no longer used: the Librarian is an Agent with its own Model now; set it with: vbot agent update librarian --model <provider/model>`.
- `notifications` sparse-merges five independent boolean switches (`run_completed`, `run_failed`, `automation_failed` for failed Cron jobs, `update_result`, `server_stopped`), all defaulting to `true` - the server only stores them, the Windows tray application reads them through `settings.values`.
- `web_search` and `web_fetch` load at call time. Web Fetch sparsely merges `provider` (direct/firecrawl/tavily/exa/parallel), `mode` (fallback/prefer) and `browser` (boolean, default true: the local browser fallback, `tools/web_fetch.md`), defaults to direct fetching, and keeps API keys outside Settings; configuration facts live in `core/fetch_config.py`.
- `archive.retention_days` (sparse) is an integer 1-3650 or `null`, default 30: `null` keeps archive entries until they are purged by hand, while unsetting the path restores 30; `settings.update` and raw-file validation refuse other values with `... must be an integer from 1 to 3650, or null to keep archived items until they are deleted`, `settings.patch` with its generic bound and type messages. The archive reads it per use, and a change wakes the retention sweep (`archive.md` -> Retention).

## Sub-Agent limits

Sub-Agent limits are flat raw keys exposed as `subagents.*` paths: `max_subagent_depth` (default 2), `max_active_subagents` (default 8, the Sub-Agents of one Session working at the same time; its own only, not theirs) and `max_active_subagents_total` (default 50, Sub-Agents working at the same time across the app). `settings.update` writes all three together. The former `max_subagents_per_turn` and `subagent_timeout_minutes` were removed; stored values only warn as unknown keys, are left out on load and kept in the file by writes. How the limits refuse a `run`: `subagents.md`.

## `base` plumbing

The `base` contract itself is in `settings.md` -> Public surface. `settings.update` parses it with `parse_settings_update_base`; for `task_model.update` it is `{"model_tasks": {...}}`, where a binding with an empty target means unbound. Inside the write transaction (`StorageManager.update_settings_sections`, which `update_model_task_settings` uses) `stale_setting_paths` (`core/storage/_settings_updates.py`) compares effective values (`build_effective_settings`) leaf by leaf. `task_model.update` validates each binding that differs from the stored one, so a caller sends only the bindings it changed. Tests: `test_section_update_with_base_refuses_only_changes_over_newer_values`, `test_update_based_on_stale_values_is_refused_without_effects`.

## Installer seed of `defaults.agent`

The installer (`cli/application/install.py`, `FRESH_AGENT_DEFAULTS`) seeds the `settings.json` of a data directory it creates with thinking effort "high" only and leaves temperature and top_p unset, so Chat sends neither and the Provider's own default applies (`chat.md`). Install-time seed only - existing instances keep their configured default until it is cleared; this is not a runtime fallback.
