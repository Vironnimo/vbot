# Model Usage

`core/usage/` owns durable per-request accounting in `<data-dir>/model-usage.db`.
Statistics consumes it as a source; the Statistics index remains disposable.

## Ownership and lifetime

The request lifetime differs from the Session lifetime: Task Models and background
requests may have no Session, and archiving or deleting a Session must not erase
already incurred consumption. `UsageRecorder` therefore owns its canonical
database independently of Sessions, Model catalog pricing, and Provider quota
observations (`providers/usage.md`). Runtime constructs one recorder and injects
it into the request owners and Statistics.

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
- `read_since(revision)` returns the current versions of changed calls and the
  watermark from one database snapshot. Statistics tracks database identity,
  revision and the read-only `projection_epoch`, which is fresh on every recorder
  initialization. Reopening after restore rebuilds the projection even when
  newly recorded calls have already caught up to its previous revision.
- `import_session_history(source)` incrementally ingests retained Session own
  audit, including archives and superseded entries. The named `session-history-v1`
  source cursor and imported rows commit together. The first import of each
  Session database handle replays from zero, including a cursor reset for empty
  restored sources; later imports on that handle use the persisted cursor.
  Embedded `usage_call_id` deduplicates requests and recovers missing counters
  or replaces estimates with measured Session evidence after restore. Existing
  measurements, Provider costs, attribution and failed/cancelled outcomes remain
  authoritative. An interrupted Assistant never proves completed usage. Legacy
  identities use source database, Session generation, entry key and Message id
  so restored key reuse and duplicate Message ids remain distinct. Fork
  inheritance and materialized inherited prefixes do not enter the export.
  Runtime imports before any Agent lifecycle work; Statistics catches up before
  reads.

## Persistence and failures

`_schema.py` declares canonical Generation 1 tables through `core/database/`.
Runtime and offline tools register `model_usage`, so snapshots, health and recovery
include it. Every changed call receives a monotonically increasing revision;
there is no Session-triggered deletion or retention pruning.

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
