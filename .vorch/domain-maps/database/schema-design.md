# Schema design rules

Design rules for every Generation 1 table and later addition in a kernel database, core or Extension. They complement the additive rules of `../database.md` -> Evolution contract, which decide whether a change may ship at all.

- The format generation is stored three times: `PRAGMA user_version`, `kernel_meta.format_generation` and the database's marker entry; generation 1 is current.
- Applied data migrations are recorded only in the Migration ledger `kernel_migrations` (one row per migration: `name`, `applied_at`, `applied_by_version`, `breaks_older`), never in ad-hoc version keys in owner tables.
- No enum CHECK constraints on extensible vocabularies (roles, kinds, statuses, causes); CHECK stays for permanent invariants such as `>= 0`, `json_valid`, paired nullability.
- Hot tables stay narrow, with large payloads in 1:1 side tables.
- Integer surrogate keys for internal relations.
- JSON only for genuinely open payloads.
- Content-addressed storage for large repeated values.
- Timestamps follow the canonical-text rule in `../database.md` -> Evolution contract through their one owner `core/utils/timestamps.py` (`format_canonical_timestamp`, `parse_timestamp`, `canonical_timestamp`, `utc_now_timestamp`; aware values only, a value without an offset raises; readers of stored values use `parse_canonical_timestamp`, which raises on any non-canonical form because that can only be bad data, or `is_canonical_timestamp` when validating a whole record; `parse_timestamp` stays for incoming values such as CLI or RPC arguments). The kernel, Stores and Extensions use it instead of private copies.
- `AUTOINCREMENT` where a key feeds a derived index or projection.
- Every index has a named reader.
- Growing values are appended as chunk rows, never rewritten.

Tests of the evolution contract: `tests/core/database/test_database_evolution.py` (incl. an older-binary simulation); Session integration in `tests/core/sessions/test_store_database.py`.
