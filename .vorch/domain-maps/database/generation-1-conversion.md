# Generation 1 conversion

The one-time offline conversion of a data directory written by vBot before persistence Generation 1 (the 0.4.x releases) into Generation 1: `scripts/converters/persistence_generation_1/`. Normal startup never converts (`database.md` -> Evolution contract, item 6).

## Operator procedure

Run once per data directory, from a vBot checkout that includes Generation 1:

1. Stop vBot completely: server, desktop shell and tray application. The converter refuses while any server claims the data directory.
2. Back up the data directory with the usual backup mechanism. The conversion keeps every original file (step 6), but only an independent backup protects against a failing disk.
3. Dry run, with the report written outside the data directory:

   ```bash
   python -m scripts.converters.persistence_generation_1 <data-dir> --dry-run --report <file>
   ```

   It converts and verifies into a staging directory, prints a summary (counts per area, verification, skipped or approximated items, sizes) and discards the staging directory. The data directory keeps its content; only an empty `data-store.lock` remains. Read the skipped items and the JSON document errors before installing.
4. Install: the same command without `--dry-run`.
5. Start a vBot that includes Generation 1, never the old release: it finds no `session-store.json` and refuses to open its Session store. Check Agents, Projects, Sessions and Channels, then take the first data snapshot: `vbot data-store snapshot create --reason manual` (status reports `snapshot_degraded` until one exists).
6. `pre-generation-1/` holds every file the install replaced or retired, at its original relative path, plus `conversion-report.json`. vBot never reads it. It includes old copies of credential files such as OAuth tokens, so it needs the data directory's protection. Delete it once the converted instance has worked for a while and a data snapshot exists.

Going back (loses everything written since the install): stop vBot, delete the paths the report lists under `install.installed` and `data-store.json`, then move the content of `pre-generation-1/` (except `conversion-report.json`) back to the same relative paths.

## Phases

`conversion.convert_data_directory(data_dir, *, dry_run)` returns the JSON report; `__main__` prints the summary and exits 1 on a refusal, a failed conversion or an interrupted install (130 on Ctrl+C).

1. Preflight refuses with nothing changed (`_preflight.py`): no vBot data directory; a running server (lifetime lock claim `runtime/server-<port>.lock` or a control record whose process runs with the recorded creation time); a maintenance guard of another operation, or of a conversion whose process still runs; `data-store.json` present (already Generation 1, or an unreleased development build between the kernel and Generation 1 with a pre-Generation-1 `sessions.db`, which is unsupported); an existing `pre-generation-1/`; an unsupported source shape (area `check_source`); too little free space (2.5 x the rebuilt databases + the provider-usage JSONL + 256 MiB).
2. Under the maintenance guard `data-maintenance.json` (operation `generation-1-conversion`), the areas run in `AREAS` order into `generation-1-staging/files/` inside the data directory, on the same volume: `json_documents`, `decisions`, `swarm`, `provider_usage`, `channels`, `sessions`, `mcp` (it attaches saved MCP results to Tool calls in the staged `sessions.db`). Every source is read only.
3. Verification (`_session_check.py`, `_verify.py`):
   - Sessions: every Session's staged view equals its legacy active history by entry id, unless a reported skipped item names one of the view's source Sessions; the longest live Session and the longest live shared fork are loaded through `ChatSessionManager`.
   - Every staged `*.db` is opened offline with its spec (the Swarm spec included) and checked with `verify_database_file`; its journal is checkpointed away.
   - JSON documents are validated by their owners as `vbot doctor config` does. A staged document without a valid `format_version` fails; errors in content carried over unchanged are reported for the owner to repair.
4. A dry run stops here. An install first refuses when a source changed after the conversion started (a vBot process may still use it), then writes `generation-1-staging/install-plan.json`.
5. Install (`_install.py`), all renames within the volume: every replaced or retired file (sources at staged paths, retired legacy files such as `session-store.json` and `session-snapshots/`, and the `-wal`/`-shm`/`-journal` files of rebuilt databases) moves to `pre-generation-1/`; the staged files move into place; `write_marker_for_databases` registers every installed canonical database; each is opened offline and checked against its registration; the report is written to `pre-generation-1/conversion-report.json`; the guard is released last.

## Interruption

- Before the install plan exists, any failure or Ctrl+C discards the staging directory and releases the guard; the source is untouched.
- An interrupted install keeps the guard, so vBot refuses the data directory. Running the same command again finishes it: each step checks whether it already happened (a retired file already in `pre-generation-1/`, a staged file already moved), then registration and checks repeat. A dry run is refused while an install is pending.

## Supported sources

- `sessions.db`: application id 0, `VBOT` or `VBSS` with the frozen legacy tables and columns (`sessions.py`); an older incomplete shape asks to start the last release before Generation 1 once. A leftover `session-store-maintenance.json` refuses. Assistant Messages from before field-level Usage provenance, output-file spans and stored Context snapshots are normalized (rules in `sessions.md` -> Storage Contract; report counts `usage_provenance_derived`, `output_file_spans_derived` and `context_snapshots_derived`, each dropped reference and each Usage left without a snapshot a skipped item). A file already in the Generation 1 shape stays in place and is adopted on the first open.
- `decisions.db` and `extension-data/swarm/swarm.db`: application id 0 and `user_version` 0 with the frozen legacy columns, or already Generation 1; any other identity refuses.
- JSON documents, Channel state files, `statistics/provider-usage/*.jsonl` and the MCP Extension's `mcp/connections.json` and `mcp/content/`: the per-area rules in `settings.md`, `channels.md`, `providers/usage.md` and `extensions/mcp.md` (`scripts/converters/persistence_generation_1/mcp.py`). Retired Tool names in persisted Tool access: see Retired Tool names below; other retired JSON fields and values: see Retired fields and values below.

Files no area reads (for example old `*.before-*.db` copies) stay in place untouched.

## Retired Tool names

The application knows only current Tool names: a retired name in an Agent or override policy grants and denies nothing, and temporary Session bindings and Swarm profiles refuse it. `_tool_access.py` therefore rewrites every persisted Tool-name list the converter stages: Agent `tool_access` (also the one converted from a legacy root `allowed_tools`), Project `allowed_tools` whitelists, Project Agent override `tool_access` (`json_documents.py`), `tool_access` in temporary Session binding configs (`sessions.py`), and the Swarm Extension's saved profiles (`profiles.payload`) and started Swarms' `swarms.profile_snapshot` (`swarm.py`). Archived Agents and Projects under `archive/` are not read by the application and are not converted.

| Retired | Successor | Successor granted when the retired Tools available covered it | Retired in |
|---|---|---|---|
| `grep`, `glob` | `search_files` | both | `dd1315abe` |
| `write`, `edit` | `apply_patch` | `write` (a full-file write could create, replace and change any file; `edit` alone could not create one) | `0849f39f0` (write), `07d9e6cb0` (edit) |
| `terminal_beta` | `terminal` | always (rename) | `0f43fae2c` |
| `read2`, `read_new` | `read` | either | `bfa62986e` |
| `subagent_result` | none, dropped (`subagent` includes the status lookup) | - | `dc1459480` |
| `skill_list` | none, dropped (`skill` includes the catalog) | - | `001cb4e1d` |
| `session_read` | none, dropped (deep reads moved to the vbot-cli Skill) | - | `b8db7699f` |
| `browser` | none, dropped (moved to the playwright-cli Skill) | - | `5d25cdb26` |
| `swarm_decisions` | none, dropped (Swarm decisions removed) | - | `0019b72a0` |

Rules, none of which widens a policy:

- Allow lists (`selected` policies, Project whitelists): the successor takes the place of the first retired name it replaces when the allowed retired Tools cover it and the policy does not deny it; a list that already names it keeps it where it is. Otherwise the retired names are only removed and the narrowing is reported ("enable ... explicitly if wanted"). So `write` alone grants `apply_patch`; `edit` alone does not.
- Deny lists: the successor is denied when the retired Tools still available no longer cover it and the policy denied one of them or allowed every Tool, unless the policy explicitly allows the successor. Denying `edit` alone in a mode-`all` policy therefore keeps `apply_patch` available, as full-file writes were; denying `write` (with or without `edit`) denies `apply_patch`. A denial that removes remaining access (for example `grep` still available after denying `glob`) is reported.
- Retired names in `granted` are removed without granting the successor. Dropped names are removed from every list.
- Invalid policies stay as they are for the application to report. Converting a converted policy changes nothing.

Report: count `retired_tool_names_converted` per area (`json_documents`, `sessions`, `swarm`) and one skipped-or-approximated item per converted field, naming each replacement, drop and narrowing (for example `tool_access: edit and write replaced by apply_patch; grep and glob replaced by search_files`).

## Retired fields and values

A field or value an older vBot wrote and a later one retired or renamed is not unknown future data: the application knows only its successor and would carry the old one around as an unknown field, or ignore it. `json_documents.py` therefore maps each one to its successor, or drops it, in the documents it stages. Each exact conversion is counted; a change of behavior is also reported as a skipped-or-approximated item.

| Document | Retired | Conversion | Count (item) | Retired in |
|---|---|---|---|---|
| `agents/*/agent.json`, `settings.json` `defaults.agent` | `fallback_model` (one binding; empty = none of its own) | A binding becomes `fallback_models: [binding]` in its place; empty or `null` is dropped; next to an existing `fallback_models` it is dropped (item); a value of another type is carried over as the chain's only entry, so the application reports it as the old one did (item) | `fallback_model_converted`, `fallback_model_dropped` | `0f67dde58` |
| `settings.json` | `recall.backend` `jsonl_scan` or `canonical_scan` | `sqlite_fts`, the default Search backend; canonical scan survives only as its internal degraded fallback (item) | `recall_backend_replaced` | `54a24c183` (renamed), `b8db7699f` (no longer selectable) |
| `settings.json` | `live_voice` (`enabled` opt-in) | Dropped; the Live voice control follows the `model_tasks.live_voice` binding (item) | `live_voice_dropped` | `ee1fca647` |
| `settings.json` | `reflection.skill_tool_call_interval` (Tool calls) | Dropped, not mapped: its successor `skill_model_step_interval` counts Model steps (item) | `reflection_skill_tool_call_interval_dropped` | `3798a558b` |
| prompt `layout.json` (default and Agent scopes) | block `core:project_files` | Renamed `core:working_project` in place (dropped when the layout already names it). The same change split the host, version, Workspace and path lines out of `core:runtime` into `core:identity_runtime`, so a layout naming the retired block, and only such a layout, also gets `core:identity_runtime` right after `core:runtime` with its `enabled` state | `prompt_project_files_converted`, `prompt_identity_runtime_added` | `75d3934fa` |

Temporary Session binding configs, Swarm profiles and Project Agent overrides never carried `fallback_model` or a prompt block list naming `core:project_files`: the first two were introduced with `fallback_models` and after the block rename, and overrides never modeled a fallback.

## Tests

`tests/scripts/converters/persistence_generation_1/`: `test_conversion.py` (end to end, refusals, failure before install, changed sources, interrupted install, the CLI) and one test module per area.
