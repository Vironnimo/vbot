# Model Usage

`core/usage/` owns durable per-request accounting in `<data-dir>/model-usage.db`.
Statistics consumes it as a source; the Statistics index remains disposable.

## Ownership and lifetime

The request lifetime differs from the Session lifetime: Task Models and background
requests may have no Session, and archiving, deleting or purging a Session must not
erase already incurred consumption (an archive purge imports first, `archive.md`).
`UsageRecorder` therefore owns its canonical database independently of Sessions,
Model catalog pricing, and Provider quota observations (`providers/usage.md`).
Runtime constructs one recorder and injects it into the request owners and
Statistics.

No request/response payloads, Tool arguments or credentials are stored.
Optional Agent, Project, Session, Run and Extension/group references are
attribution snapshots without foreign keys to removable domain records.

## Interfaces

- `start(model, kind, ...scope)` durably allocates a `use_` id immediately before
  request dispatch. Model identity is `provider/model`; Connection is separate.
- `finish(id, usage, status=...)` stores known canonical counters, their per-field
  estimate provenance and a cost snapshot, returning Usage with `usage_call_id`.
  Missing counters remain absent. Provider-reported cost takes precedence over
  catalog valuation. Recording does not infer token quantities for media.
- `update(id, usage)` enriches the same call without changing its outcome.
  Cumulative Live Voice reports replace counters instead of adding repeated
  snapshots. Chat enriches its record with final estimates before appending the
  Assistant Message. Completion without new Usage retains earlier measurements.
- `read_since(revision, *, page_size=1000)` streams the current versions of
  changed calls as `UsagePage(revision, records)` pages in revision order, each
  from its own short read transaction (never one read held open while the
  consumer works, nor the whole ledger in memory). The first page always
  arrives and carries the watermark of its read; a call that changes between
  pages reappears later with its newer revision, so the last page's watermark
  covers everything delivered.
- `ledger_id` is the continuity token projections key their revision on:
  database identity plus its latest restore (`Database.restore_id`,
  `database.md`). It is stable across restarts, so Statistics continues
  incrementally; a restore changes it, so a projection rebuilds even when newly
  recorded calls have already caught up to its previous revision.
- `import_session_history(source)` incrementally ingests retained Session own
  audit, including archives and superseded entries. The named `session-history-v1`
  cursor (the last imported entry key) commits together with the imported rows
  and with the source's `database_id` and `restore_id`. An import resumes from
  that cursor, also after a restart; another Session database or a new restore
  of it (entry keys may be reused after a restore) replays from zero, and a
  replay of an empty source records the new identity with cursor 0. A restore
  of `model-usage.db` itself rewinds rows and cursor together, so resuming stays
  correct.
  Embedded `usage_call_id` deduplicates requests and recovers missing counters
  or replaces estimates with measured Session evidence after restore. Existing
  measurements, Provider costs, attribution and failed/cancelled outcomes remain
  authoritative. An interrupted Assistant never proves completed usage. Legacy
  identities use source database, Session generation, entry key and Message id
  so restored key reuse and duplicate Message ids remain distinct. Fork
  inheritance and materialized inherited prefixes do not enter the export.
  Runtime imports synchronously in bootstrap before any Agent lifecycle work or
  accessor can delete history (`Runtime.start()` is synchronous, so a
  background import would race Session deletion); Statistics catches up before
  reads.

## Persistence and failures

`_schema.py` declares canonical Generation 1 tables through `core/database/`.
Runtime and offline tools register `model_usage`, so snapshots, health and recovery
include it. Every changed call receives a monotonically increasing revision;
there is no Session-triggered deletion or retention pruning. `usage_imports`
gained the nullable `source_restore_id` additively; a row written before it
reads as "no restore", so the first import from a Session database with any
recorded restore replays once.

Request producers must finish in cleanup even on validation errors and
cancellation. Response persistence settles before repeated cancellation
propagates. After a restart, a request still marked `started` becomes
`interrupted`; its counters remain unknown unless previously reported. This
records dispatch intent, not proof that the Provider received or billed it.
Inner Adapter transport retries that return no separate Usage are not separate
observable accounting records; the shared Task client can observe individual
POST attempts. GET polling/downloads do not create Model requests.

Historical Task Model requests and deleted history with no retained Usage cannot
be reconstructed. The import never fabricates them. Current request kinds cover
Chat, Compaction, Session/group titles, Extension sampling, all Task Model types
and the delegated Live Voice backend; producer details remain in their maps.

Evidence: `tests/core/usage/test_usage.py`, producer accounting tests in Chat,
Compaction and Model Tasks, and `tests/core/statistics/test_statistics_accounting.py`.
