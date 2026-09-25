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
5. Start a vBot that includes Generation 1, never the old release: it finds no `session-store.json` and refuses to open its Session store. Check Agents, Projects, Sessions and Channels, then take the first data snapshot: `vbot data-store snapshot create --reason generation-1` (status reports `snapshot_degraded` until one exists).
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

- `sessions.db`: application id 0, `VBOT` or `VBSS` with the frozen legacy tables and columns (`sessions.py`); an older incomplete shape asks to start the last release before Generation 1 once. A leftover `session-store-maintenance.json` refuses. Assistant Messages from before field-level Usage provenance and output-file spans are normalized (rules in `sessions.md` -> Storage Contract; report counts `usage_provenance_derived` and `output_file_spans_derived`, each dropped reference a skipped item). A file already in the Generation 1 shape stays in place and is adopted on the first open.
- `decisions.db` and `extension-data/swarm/swarm.db`: application id 0 and `user_version` 0 with the frozen legacy columns, or already Generation 1; any other identity refuses.
- JSON documents, Channel state files, `statistics/provider-usage/*.jsonl` and the MCP Extension's `mcp/connections.json` and `mcp/content/`: the per-area rules in `settings.md`, `channels.md`, `providers/usage.md` and `extensions/mcp.md` (`scripts/converters/persistence_generation_1/mcp.py`). Agent, Project and Project-override Tool access replaces `grep`/`glob` with `search_files` without widening access (`_tool_access.py`; a narrowing is reported).

Files no area reads (for example old `*.before-*.db` copies) stay in place untouched.

## Tests

`tests/scripts/converters/persistence_generation_1/`: `test_conversion.py` (end to end, refusals, failure before install, changed sources, interrupted install, the CLI) and one test module per area.
