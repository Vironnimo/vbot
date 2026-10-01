# Legacy archive adoption

Task-gated detail for `archive.md`: how archives written before archive entries existed become entries. Read it when changing the migration `sessions.0001_archive_entries`, the startup adoption of archived Sessions without an entry, the Generation 1 converter's archive step, or when reasoning about `backfill` entries.

## Adoption

The Session spec's named migration `sessions.0001_archive_entries` (`core/sessions/_store_schema.py`, applying `_store_archive_backfill.py::adopt_legacy_archives`; not `breaks_older`) turns archives written before entries existed into `backfill` entries. Legacy containers under `archive/agents/<id>/`, `archive/projects/<id>/` and flat `archive/<name>/` with an `agent/` child become `agent`/`project` entries (a moved Workspace beside the Agent becomes a `workspace` tree; other content of the container is recorded as `files` trees); any other directory under `archive/` except `entries/` becomes a `files` entry. Trees are adopted oldest modification time first; each claims, among the archived Sessions of its id that no entry holds yet (an Agent tree: its global Sessions; a Project tree: every Agent's Sessions in the Project), those archived at the one instant closest to the tree's modification time within `[mtime - 2 s, mtime + 10 min]`, and records `backfill.match` (`timestamp` or `none`). Remaining archived Sessions are batched by scope (or owner group) and archive instant into `session`/`owner_group` entries. Adopted payloads without a valid `format_version` record `payload_format: "older"`: listed and purgeable, never restorable, because app code reads only the current format. Every step skips what an entry already records, so the adoption is idempotent. The Generation 1 converter calls the same function after it stages `sessions.db`, since a fresh database records the migration without running it (`database/generation-1-conversion.md`).

## Startup adoption

Startup recovery (`archive.md` -> Recovery) runs only the Session half, `adopt_unrecorded_archived_rows`: archived Sessions an older vBot wrote after a downgrade get `session`/`owner_group` entries with the same batching. It never adopts legacy trees; those exist only from before the migration ran.

Tests: `tests/core/sessions/test_archive_backfill.py`, `tests/scripts/converters/persistence_generation_1/test_sessions.py`.
