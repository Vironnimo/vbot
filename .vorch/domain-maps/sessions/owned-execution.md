# Extension-owned Session execution

Task-gated detail for `sessions.md` -> Extension-owned execution. Source: `core/sessions/_store_owned.py`, `_store_runs.py` (`admit_run`), `sessions.py`. The owner-bound creation/admission facade is `core/agents/temporary.py`; domain state belongs to the Extension. Tests: `tests/core/sessions/test_sessions_temporary.py`, `test_run_ownership.py`, `test_store_database.py`, `test_store_query_plans.py`, `test_lineage_storage.py`.

## Relations

- `temporary_session_bindings`: one row per owner-managed Session (`owner_name`, `group_id`, `participant_id`, `config_json`). `create_bound_temporary_session` creates the Session and its binding in one write, or returns the bound generation when the same participant is already bound with the same configuration; a binding of an archived Session, a conflicting configuration and an occupied address are rejected.
- `session_delivery_receipts`: per Session, owner and receipt id - content hash, effect kind, carrier kind and the carrier's seq.
- `run_execution_owners`: one row per owned Run (`run_key` unique, `UNIQUE (session_key, input_id)`), written by Run admission in the same transaction as the Run row (`sessions.md` -> Storage Contract). The owner's participant binding must be live in the generation the owner names; the Run may execute in that Session or in a descendant Session the owner continues. A mismatch fails with `This Session no longer matches this Run. Ask the user to resume it through its Extension.` `Run.wait_admitted()` lets the owner learn the Run only after that admission committed (`core/agents/temporary.py`).
- `temporary_group_titles`: one optional display title per owner/group (single line, stripped, <= 120 characters); it describes and never authorizes, survives a disabled owner, and is removed by `delete_temporary_group`.

## Authority

- Exact address plus Session generation and registered owner establish authority. Forks and materialized copies never carry a binding, receipt or execution owner.
- Carrier Messages and their delivery receipts commit together (`append_messages_with_receipts`): the generation must match, the owner must be bound to the Session, and a receipt already recorded must describe the same effect. With `deduplicate_carrier`, a single User or Note carrier whose receipt already exists is not appended again.
- Explicit entry Run membership restricts owned usage even when ordinary Runs reuse the Session.
- Owner-managed Sessions reject ordinary archive, move and delete and the Identity-Agent rename and Agent/Project archive scope mutations that would cover them (`This Session is managed by an Extension. Use that Extension to resume it.`); a fork is allowed only when the caller names a target Agent and the fork lands in another scope.
- `delete_temporary_group(owner_name, group_id)` deletes, in one transaction, only the Session generations bound to that exact owner/group (each through `delete_session`, so descendants receive materialized copies first) plus the group title, and returns how many it deleted.

## Reads

- Owned-Run reads are indexed point lookups (async-only at the manager): `owned_runs_by_id_async` resolves exact Run ids of one owner group through `run_execution_owners_group_run` (statements of at most 100 ids; ids of deleted Sessions are absent), and `owned_run_by_input_async` resolves a live Session's admitted input through `UNIQUE (session_key, input_id)`. `owned_runs` pages (1..1000 records, admission order, archived Sessions included) remain for whole-group listings.
- `temporary_bindings_async` pages one group's live bindings by participant id (1..1000); `temporary_group_titles_async` reads the titles of at most 1,000 groups.
- `list_owned_session_summaries(owner_name?, group_id?, metadata_keys)` is the one set-oriented read of live owner-managed Sessions for derived consumers (Statistics, title candidates): normalized summaries plus owner, group, group title, participant id and the binding config's display `name`/`model`, never other binding config. A group filter requires its owner.
- Ordinary Agent/Project discovery must not report the synthetic `tmp_...` participant Agents: `list_agent_ids(exclude_owner_managed=True)` (one `SELECT DISTINCT agent_id` over the live address index) backs `ProjectStore.session_owning_agents`, and `list_summaries_page` excludes owner-managed Sessions even after an ordinary metadata write (`test_temporary_session_stays_out_of_normal_lists_after_metadata_rewrite`).
