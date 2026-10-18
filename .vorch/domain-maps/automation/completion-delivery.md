# Completion Delivery

Read when changing how `TriggerService`'s completion coordinator delivers background Bash, Terminal and Sub-Agent results to a Session: submission, delivery at Model-request boundaries and in follow-up Runs, withdrawal and delivery claims, Extension-owned notices, and close. The contract summary is in `../automation.md` -> Interfaces.

## Submission and delivery

- Producers submit uniquely identified Bash/Terminal/Sub-Agent results (`submit_completion`) whose Future resolves only when durably persisted into a combined completion note; terminal exceptions mean no durable target exists. `wake=False` (the Sub-Agent takeover notice) persists the notice without starting a Run for an idle Session; an active Run still reads it at its next boundary. Chat's async request-boundary seam (`deliver_background_completions`) commits ready results as one Note under the Session write lock and returns that committed Note for request projection. Chat first commits earlier deferred Notes to preserve their order; the completion append bypasses deferral, settles before cancellation and acknowledges producers only after commit. A failed append releases its delivery claim and retains the notices for retry. Their Futures resolve then, but a waking notice stays pending until the Model answered that request (the Run's `iteration_count` grew past its value at the join). If the Run ends first (it failed, was interrupted, or was cancelled other than by the user), the notice wakes the Session like an unconsumed one: the follow-up Run's message replaces the results, already in history, with `UNREAD_COMPLETION_TEXT`, so they are never stored twice; a user's Stop starts no Run (`test_completion_delivery_joins_active_run_at_next_request_boundary`). `cancel_completion` withdraws a notice when an equivalent manual Result reached history first, and only until a delivery claims it (`_CompletionNotice.claimed`): the claim is set right before the message carrying the notice is built (a follow-up Run after its reply-surface lookup, a completion note under the Session write lock once the Session is loaded) and released when that message does not reach the Session (admission blocked, `ActiveRunError`, a start error, a failed append, or a Run that ended before its input was saved). A refused withdrawal returns False and the result is delivered once, as submitted (`test_a_result_its_delivery_claimed_cannot_be_withdrawn`, `follow_up_run` and `reminder` cases). When an idle Session needs a follow-up Run, the coordinator recovers its latest persisted `ReplySurface` through Sessions' newest-prefixed-Note read rather than loading history; Runtime injects the Channel relay so a Channel-surface result is delivered through the ordinary adapter instead of remaining transcript-only.
- `TriggerService` keeps the producer facade; `_completion.py` owns notice state, coalescing, persistence and delivery. Internal submission and withdrawal carry the existing `SessionAddress` intact; only actual transport edges serialize its fields. Background completions key exactly `(project_id, agent_id, session_id)` and capture their active Run: ready results inject at Model-request boundaries without extra requests or split Tool cycles; unconsumed notices deliver in one internal follow-up Run, recovering and persisting the latest `ReplySurface`, or persist as a System Reminder when that cannot start (the append runs on the Sessions worker pool; read and append failures retry capped at 30s, while a typed missing Session fails delivery terminally). Blocked admission (a Run Admission Guard) is not a start failure: it does not announce its end, so the notices join a still-active Run of the Session or retry the start with the same 0.25s->30s backoff, logged per attempt at DEBUG (`test_blocked_completion_admission_backs_off_until_admission_reopens`). A configured relay consumes the follow-up Run events for accessor delivery while the coordinator still owns durable note acknowledgement. Every completion message opens by stating it is not a new request and the Agent must re-evaluate the original goal; user-cancelled origins append notes instead of starting Runs - no cancellation-triggered cascades.

## Extension-owned notices

Extension-owned completion notices additionally retain their exact execution owner.
Submission checks that owner through the injected owned-completion validator
(Runtime wires the temporary-group Run admission check) and rejects a closed epoch,
replaced generation, or retired registration before any state is kept; without a
validator, owned notices are rejected. The Session-write fallback for a
user-cancelled origin checks the owner again, because that write bypasses Run
admission. Admitted notices join only a matching owned Run's request boundary or use
the injected owned continuation starter; admission failure never falls back to an
arbitrary Session write. A blocked admission re-checks the owner: a stale owner (closed
group, retired Extension) fails its notices terminally like the fallback instead of
retrying. Producers treat that `RunAdmissionBlockedError` as the expected end of the
owner's lifecycle: Sub-Agent forwarding (`core/subagents/forwarding.py`) and Terminal attention delivery leave the
result undelivered and log it at DEBUG without a traceback, while any other terminal
failure logs ERROR (`tests/core/tools/test_terminal_manager_attention.py`). Group closure withdraws only matching pending notices and
keeps no per-group state: late submissions fail validation, which matters because a
Terminal's stale activity owner can submit long after the group closed. Unrelated
notices retain normal delivery behavior (`_completion.py`,
`tests/core/automation/test_automation_completion.py`,
`tests/core/runtime/test_runtime_extension_host.py`).

Shell crash recovery does not submit an obsolete Extension execution epoch to
this coordinator. Its durable command checkpoint authorizes only an outcome Note
in the same live Session generation, committed with a canonical receipt through
Sessions; it never admits a Run or revives the old group (`tools/shell.md`).

## Process-local state and close

- Completion buckets, dedup, retries, and remembered cancelled origins are process-local; delivery waits at Run boundaries rather than queuing one Run per producer. Once the fallback note persists, the reminder itself is durable across restarts.
- Closing completion delivery (`TriggerService.aclose`) persists every still-pending notice without an owner as that System Reminder in one attempt (failure logs ERROR and drops them) and withdraws owned ones, so a result still waiting when the server stops, for an update too, survives the restart. Runtime therefore closes it before canonical Sessions. A worker cancelled mid-append settles that append first, so close never writes it twice (`test_completion_delivery_aclose_persists_pending_results_and_rejects_later_ones`, `test_closing_during_a_reminder_write_persists_it_once`). A follow-up Run admitted just before close that had not yet persisted its input may still persist it: at-least-once, not exactly-once.
