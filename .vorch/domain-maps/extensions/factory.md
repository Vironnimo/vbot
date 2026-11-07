# Software Factory

The bundled `resources/extensions/factory/` Extension carries Project work from Cards through the stages of a per-Project Workflow (plans, drafts, isolated changes, independent reviews) to the user's acceptance and integration; it loads through ordinary bundled discovery (API 25).

## Terms

Shared Agent, Session, Run, Tool and Project vocabulary lives in GLOSSARY.

### Card
One unit of Factory work with an objective, Project, provenance, lifecycle and participant Sessions. Not a Session, Run or Session Goal.

### Board
The Factory page showing Cards in lifecycle columns, with `Waiting for you` as the review inbox.

### Workflow
A Project's named, ordered list of Stages that a Card runs through; a setup holds several, one is the default, and a Card snapshots the one it starts with. Not a GitHub workflow or a Skill.

### Stage
One step of a Workflow. A work Stage hands in an Output; a review Stage judges the Output of one earlier work Stage (its target) and can send it back for rework. An approval Stage waits for the user before the next one starts.

### Lane
One participant slot of a Stage: an Agent (roster or Team), optional Model and effort. Lanes of a Stage do the same task independently, one after another.

### Output
A recorded hand-in of a work lane (`document`: text in `result`; `changes`: a Git-verified branch range) or a review verdict. Later Stages read the latest Outputs of earlier Stages.

### Integrator
The single lane of a Workflow's last `changes` Stage; it integrates the result after the user accepts. A Workflow without one ends at acceptance.

### Project setup
The Factory's Project-specific Workflows, default Workflow and execution limits (turn budget, base branch, integration check).

### Origin
The recorded source of a Card, deciding whether it starts automatically or waits as a Proposal.

## Ownership

- `_registration.py` declares the page, public and private Tools, settings, operations and required Session runtime callbacks; import and registration acquire no resources.
- `extension.py` owns the single serialized scheduler, host lifetime, change publication and attention. `_management.py` owns operator actions and read projections. `store.py` owns domain persistence, not canonical Session storage.
- `_execution.py` owns participant configuration, phase admission, submission/review/integration guards, budgets and reconciliation. `_git.py` owns bounded read-only Git verification; Project instructions, not Factory code, own worktree creation and integration commands.
- `agent_text.py` owns Agent-facing definitions, instructions, notes and results. `ui/` owns page state and presentation through the generic Extension bridge; assets build to `web/` with the WebUI build.
- The existing Extension host is the closest owner for user attention from Extensions, current-input authority and owned approval visibility: these require current registration and canonical Run identity, not a Factory-local notification or approval store (`extensions/host.md`).

## Persistence and transitions

The host opens `ext.factory.factory`; tables are STRICT, additive, with explicit SELECT/INSERT columns and code-validated states. Setups (Workflows as JSON), Cards, Outputs, comments and audit events live here; participant history, receipts and usage remain canonical host reads. A Card change, its event and any attached comment/Output commit in one database transaction. Schema changes stay additive: the `workflows` migration (`store.MIGRATIONS`) converted role-based `project_setups` and `reviews` into `setups` Workflows and Outputs once; the old tables remain unread. Revision checks prevent stale operator drafts overwriting a newer record. Acceptance requires the displayed Card's `expected_revision`, so a stale action cannot authorize a newer submission.

Origins are host-attributed, never a Tool argument. Operator operations queue; `factory_card` queues current human requests as `user` and currently authorized tasks as `automation`. Sub-Agents, owned participants and other initiative produce `agent` Proposals. `host.review_context` is unsuitable for this decision because it includes old and ancestor requests; API 24's `input_authority` reads the exact live Run instead (`core/runtime/_shell_review_context.py`). Provenance stores the source Session address and Run id.

The lifecycle is `proposal -> queued -> working -> reviewing -> waiting -> integrating -> done`; `working`/`reviewing` cover whichever Stage and lane is current (`runtime.stage`, `runtime.lane`). Changes requested return to the target Stage and rerun every Stage after it: existing participants get a `refresh` note with the new results, new ones an initial input. Waiting reasons `checkpoint` (approval Stage), `finished` (all Stages done) and `no_agreement` (review rounds exhausted) are acceptable; Accept continues to the next Stage, integrates through the Integrator, or marks a Workflow without one `done`. Send back can target any Stage; an answer resumes the blocked participant. Failures and exhausted budgets wait for the user instead of retrying forever. Discard is terminal, with one bounded cleanup turn per existing `changes` participant, chained in Stage order; deletion of a final Card archives its owned group.

The scheduler uses one in-process lock, selects lowest-position enabled-Project work, and counts `working`, `reviewing` and `integrating` against `wip_limit` (default 2). Waiting does not count. `max_waiting` defaults to 0 (unlimited). Operator resumes also respect WIP: accepted/retried/sent-back Cards queue their intended phase when slots are full. Those Cards expose `queued_action` and cannot edit their already reviewed objective while waiting. Live Extension configuration callbacks rerun admission without polling.

Cards retain their Workflow, settings and Project-directory snapshot in runtime metadata, along with admitted participant Run ids, phase delivery state and recovery evidence. Editing the setup does not change a started Card's Workflow. Setup deletion refuses unfinished Cards. Cards started under the role-based Factory (runtime `setup`, no `workflow`) are carried over at reconcile: with nothing handed in they requeue, otherwise they wait as `run_failed` on the Project's default Workflow with their old `worker` participant kept for discard cleanup. A Card whose start failed before it chose a Workflow requeues on Retry or Send back.

Preparing a continuation never consumes its pending intent: repeated request callbacks return the same delivery, and acknowledgment/startup reconcile its exact canonical Session receipt before clearing it. A newer canonical root Run that fails before its request callback pauses the matching handoff for an operator retry, retaining undelivered guidance; sibling, older, foreign-generation and stale-epoch completions cannot claim it. Admission and pre-receipt participant failures preserve guidance for explicit retry, whose one-shot authorization is consumed before admission: an already failed canonical input gets a fresh continuation on the same History, while a crash fails closed and any new failure needs another user retry. Cleanup recovery admits a never-admitted intent once through its existing input identity and only closes an already admitted cleanup. Integration reviews pin the captured candidate and its review baseline, even after that candidate reached the mutable base branch.

## Execution contracts and verify-first findings

- One execution group per Card (`group_id == id`). Work participants are `<stage>-<lane>`, review participants `<stage>-<lane>-r<n>` with a per-Stage sequence, so every review run is a fresh participant. Each lane names any roster Agent (`source` `agent` -> `identity_profile`) or a member of the Project's Team (`team` -> `repository_profile`); `_workflows.profile` maps it. The Profile plus explicit `project_id` propagates the Project Context and its AGENTS instructions. A cwd alone does not select a Project. Prompt inspection shares execution's resolution.
- Setup reads and previews return an empty effective Model for a lane whose Agent resolves none (keyed `<workflow>/<stage>/<lane>`); saving refuses it as `model_unresolved` naming the Stage. Validation (`_workflows.normalize`) requires a review to target an earlier work Stage and the Integrator Stage to have exactly one lane. New setups default to `enabled` (Cards can be created and queued Cards start).
- A repository profile previously overrode the owner's mandatory fallback Model. API 24 permits an empty fallback only with a profile, inherits its effective Model, and adds deliberate Model/effort overrides after resolution; existing Swarm configurations retain their semantics (`agent/temporary-agents.md`).
- All private Tools of an owner are Session grants; each participant gets only its Stage's Tools (review: `factory_submit_review`; Integrator: work plus `factory_report_integration`). Review and `document` Stages also deny `apply_patch`, `edit`, `write` (the latter two hidden followers, so the public catalog alone is incomplete). Shell remains available for Git/tests. Read-only stages are instruction scope, not an OS sandbox: the shell can still write, and ordinary Project Permission Rules remain unchanged.
- `run_finished` carries binding, participant, Run id and outcome, but fires before the Run releases its Session. Admission/drain for the next phase runs in tracked work after that release, not inline in the completion callback. A successful private Tool uses `context.request_turn_end()`; no extra Model-facing channel is introduced.
- Temporary groups refuse participant creation while open. Fresh participant admission therefore drains/closes/reopens through the existing owner API rather than bypassing that guard. Late callbacks and obsolete participants' Tools cannot move a newer phase.
- `factory_submit_work` in a `changes` Stage resolves the submitted local branch and base, requires actual commits, and records the merge-base and head; in a `document` Stage it requires `result` (at most 100000 characters). Git uses fixed argument lists, validated names, timeouts and bounded output, never shell interpolation. The next participant's input already includes the Output recorded by the same transition.
- A review Stage's `rounds` counts its total automatic reviews including the first, not reworks (templates use 2-3). Review lanes of one pass judge independently: a later lane sees only findings of earlier passes. Integration reviews count separately; user send-back resets the counts. A reviewer gets one nudge and one fresh-participant fallback before `review_failed`. Work failures wait immediately; successful but non-submitting turns consume the per-lane turn budget (default 30).
- Acceptance authorizes only the reviewed head and, for `merged`, captures the current target commit for the Integrator and later inspection. Changed commits, including a rebase, require another review before landing. A changed, unlanded head is compared with the merge-base of the captured current target, not the old review pin. Related rewritten history is allowed for a fresh review; a diverged explicit pin is `review_base_changed`, not `unrelated_history`.
- Verified post-landing reviews expose limited landing evidence and its verdict on the Card. Requested changes for the current landed candidate pause immediately as `waiting/no_agreement`, even with automatic rounds left; only the user decides whether to override, send back or discard. A later isolated submission keeps the earlier landing warning as history without inheriting its post-landing behavior. Discard/rework never revert the target automatically. A PR URL or `reference_only` still records a reference without asserting local ancestry; unchanged reference-only work does not require a local target lookup.
- Pending permission requests are read through an owner-scoped host capability and link to the existing Session approval UI; Factory neither auto-approves nor widens rules. The page refreshes on approval invalidation rather than polling.
- Startup reconciles Cards and owned groups, recovers interrupted phases with bounded continuation and archives orphan groups. Quiesce drains owned work before registration retirement. Closed done/discarded groups retain readable Sessions until Card deletion.

## Attention and page

Entering `waiting` or receiving a Proposal sends an owner-keyed transient notification; both waiting Cards and Proposals count toward the `board` page badge. A missing recommendation, question and note falls back to a readable Board-directed sentence, never a raw reason code. Closing a group invalidates its Card again after the Run ends, so cached status and usage do not remain live without another Card mutation. The Board is the durable attention record. Generic notification validation, event routing, Settings switches and retirement behavior belong to the host/Accessor maps, not this store.

Routes are `''`, `/card/<id>` and `/setups[/<project_id>]`. The page uses shared navigation, controls, autosave and its `factory.*` catalog; owner invalidations refresh data without polling or replacing active drafts. Card detail includes Stage Outputs (documents, review recommendations/findings, summary/checks, range), event history, usage, participant History/Run streams and normal permission-approval links. Already-landed work has a prominent notice, compact Board status and copyable landing/extra-path/merge-path evidence; the discard confirmation warns that it does not revert the target. A pending unknown baseline is not mislabeled as an earlier historical submission.

## Agent-facing text

Before changing Factory text in `agent_text.py` or the Factory section of `vbot-docs/references/extension-usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. Stage rules, the user's Stage instructions and continuation data are separated and labeled; continuations become canonical notes, never fabricated user instructions.

## Verification routes

- Persisted Card/setup/audit contracts: `tests/resources/extensions/factory/test_factory_store.py`.
- Public Tool origins, operator guards and scheduler admission: `tests/resources/extensions/factory/test_factory_operations.py`.
- Execution, stale roles, budgets and restart: `tests/resources/extensions/factory/test_factory_execution.py`; integration boundaries: `test_factory_execution_integration.py`; read-only Git facts and real graphs: `test_factory_git.py` in the same directory.
- Rendered Board, actions and recommendations: `webui/src/components/__tests__/FactoryPage.test.js`; shared notification/bridge and host tests live with their respective owners.
- Real Model evaluation uses the development instance and a disposable registered Git Project; verify repository state and canonical participant calls, not the Models' completion claims (`vbot-agent-eval` Skill).

## References

Read these only when your task matches - not by default.

- Changing integration inspection, landing detection or `_git.py` integration facts -> `extensions/factory-integration.md`
- Changing Factory Agent-facing text -> `extensions/factory-agent-facing-text.md`
