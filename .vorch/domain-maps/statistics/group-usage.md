# Extension group usage projection

How `StatisticsService.group_usage` (the Extension API behind the Swarm UI's `swarms.usage`) restricts the index to one owner/group. It still runs on the pre-v3 report stack listed in `statistics.md` -> Report sections.

`group_usage` uses canonical Sessions Run-owner records and explicit Message Run ids
to restrict the existing index and aggregators to one owner/group, optionally one
participant. A group report returns participant breakdowns from the same indexed
index read and Run slices; clients need no per-participant report fan-out. The
read reconciles only the owner's Sessions and never prunes normal Statistics
scopes; `materialize_run_slices` then copies each owned Run's rows, selected by
explicit Run id through the `stat_records_run` index, into same-named temp tables
keyed by slice so `GroupReportBuilder` aggregates them. Unrelated Sessions
are never loaded, and Messages from other Runs in a reused Session are excluded by
identity. The facade runs on the index database's worker pool and
returns the existing usage, Tools, Compaction and Run projections without costs,
account data or separate counters. With the recorder, usage selects the same
explicit Session/Run identities from durable requests, including auxiliary work;
group-level requests without Run membership remain only in global accounting.
Durable group selection drives indexed reads from the requested Run slices,
then recovers each selected record's source Session key before reading its call
through the existing `(session_key, seq)` index. Unrelated request history is
neither scanned nor temporarily indexed for each group or participant report
(`test_statistics_accounting.py`).
A slice without any exact ledger match keeps its saved Session usage, including
after an Agent takeover. Durable attribution stays at the original address;
there is no canonical address alias to link auxiliary requests across a takeover,
and a Run id alone never supplies one.
Generation checks and own-audit ingestion
remain in force (`statistics.py`, `index.py`; `test_statistics_groups.py`,
`tests/core/sessions/test_sessions_owner_managed.py`).
