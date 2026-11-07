# Software Factory

The bundled `resources/extensions/factory/` Extension carries isolated Project work from Cards through a Worker, independent review and an operator's integration decision; it loads through ordinary bundled discovery (API 24).

## Terms

Shared Agent, Session, Run, Tool and Project vocabulary lives in GLOSSARY.

### Card
One unit of Factory work with an objective, Project, provenance, lifecycle and participant Sessions. Not a Session, Run or Session Goal.

### Board
The Factory page showing Cards in lifecycle columns, with `Waiting for you` as the review inbox.

### Worker
The Card's owned participant that implements an isolated change and integrates it after acceptance.

### Reviewer
An independent owned participant judging a submission without the Worker's transcript. Every review round uses a fresh participant.

### Submission
The Worker's structured hand-in: branch, base, exact commit range, summary and checks.

### Review round
One independent judgment of a Submission. Integration reviews are distinguished from rounds spent resolving automatic rejections.

### Project setup
The Factory's Project-specific participant profiles, Model choices and execution limits.

### Origin
The recorded source of a Card, deciding whether it starts automatically or waits as a Proposal.

## Ownership

- `_registration.py` declares the page, public and private Tools, settings, operations and required Session runtime callbacks; import and registration acquire no resources.
- `extension.py` owns the single serialized scheduler, host lifetime, change publication and attention. `_management.py` owns operator actions and read projections. `store.py` owns domain persistence, not canonical Session storage.
- `_execution.py` owns participant configuration, phase admission, submission/review/integration guards, budgets and reconciliation. `_git.py` owns bounded read-only Git verification; Project instructions, not Factory code, own worktree creation and integration commands.
- `agent_text.py` owns Agent-facing definitions, instructions, notes and results. `ui/` owns page state and presentation through the generic Extension bridge; assets build to `web/` with the WebUI build.
- The existing Extension host is the closest owner for user attention from Extensions, current-input authority and owned approval visibility: these require current registration and canonical Run identity, not a Factory-local notification or approval store (`extensions/host.md`).

## Persistence and transitions

The host opens `ext.factory.factory`; tables are STRICT, additive, with explicit SELECT/INSERT columns and code-validated states. Cards, reviews, comments and audit events live here; participant history, receipts and usage remain canonical host reads. A Card change, its event and any attached comment/review commit in one database transaction. Revision checks prevent stale operator drafts overwriting a newer record. Acceptance requires the displayed Card's `expected_revision`, so a stale action cannot authorize a newer submission.

Origins are host-attributed, never a Tool argument. Operator operations queue; `factory_card` queues current human requests as `user` and currently authorized tasks as `automation`. Sub-Agents, owned participants and other initiative produce `agent` Proposals. `host.review_context` is unsuitable for this decision because it includes old and ancestor requests; API 24's `input_authority` reads the exact live Run instead (`core/runtime/_shell_review_context.py`). Provenance stores the source Session address and Run id.

The lifecycle is `proposal -> queued -> working -> reviewing -> waiting -> integrating -> done`, with automatic review rework returning to `working`, and user send-back/answer resuming the same Worker. Failures and exhausted budgets wait for the user instead of retrying forever. Discard is terminal, with one bounded Worker cleanup turn when a Worker exists; deletion of a final Card archives its owned group.

The scheduler uses one in-process lock, selects lowest-position enabled-Project work, and counts `working`, `reviewing` and `integrating` against `wip_limit` (default 2). Waiting does not count. `max_waiting` defaults to 0 (unlimited). Operator resumes also respect WIP: accepted/retried/sent-back Cards queue their intended phase when slots are full. Those Cards expose `queued_action` and cannot edit their already reviewed objective while waiting. Live Extension configuration callbacks rerun admission without polling.

Cards retain their setup and Project-directory snapshot in runtime metadata, along with admitted participant Run ids, phase delivery state and recovery evidence. Editing the setup does not rewrite an already bound participant's immutable configuration. Setup deletion refuses unfinished Cards.

Preparing a continuation never consumes its pending intent: repeated request callbacks return the same delivery, and acknowledgment/startup reconcile its exact canonical Session receipt before clearing it. A newer canonical root Run that fails before its request callback pauses the matching handoff for an operator retry, retaining undelivered guidance; sibling, older, foreign-generation and stale-epoch completions cannot claim it. Cleanup recovery admits a never-admitted intent once through its existing input identity and only closes an already admitted cleanup. Integration reviews pin the captured changed head and the saved review base, even after that head reached the mutable base branch.

## Execution contracts and verify-first findings

- One execution group per Card (`group_id == id`), participant `worker`, independent `reviewer-N` participants. `repository_profile` plus explicit `project_id` propagates the Project Context and its AGENTS instructions. A cwd alone does not select a Project. Prompt inspection shares execution's resolution.
- A repository profile previously overrode the owner's mandatory fallback Model. API 24 permits an empty fallback only with a profile, inherits its effective Model, and adds deliberate Model/effort overrides after resolution; existing Swarm configurations retain their semantics (`agent/temporary-agents.md`).
- All private Tools of an owner are Session grants; each role denies the other role's Tools. Reviewer file-editing denials are exactly `apply_patch`, `edit`, `write` (the latter two hidden followers, so the public catalog alone is incomplete). Shell remains available for Git/tests. Read-only review is instruction scope, not an OS sandbox: the shell can still write, and ordinary Project Permission Rules remain unchanged.
- `run_finished` carries binding, participant, Run id and outcome, but fires before the Run releases its Session. Admission/drain for the next phase runs in tracked work after that release, not inline in the completion callback. A successful private Tool uses `context.request_turn_end()`; no extra Model-facing channel is introduced.
- Temporary groups refuse participant creation while open. Fresh Reviewer admission therefore drains/closes/reopens through the existing owner API rather than bypassing that guard. Late callbacks and obsolete Reviewer Tools cannot move a newer phase.
- `factory_submit_work` resolves the submitted local branch and base, requires actual commits, and records the merge-base and head. Git uses fixed argument lists, validated names, timeouts and bounded output, never shell interpolation.
- Automatic rejections consume the configured rounds (default 2); user send-back resets that budget. A Reviewer gets one nudge and one fresh-participant fallback before `review_failed`. Worker failures wait immediately; successful but non-submitting turns consume the per-phase budget (default 30).
- Acceptance authorizes only the reviewed head. A moved submission head triggers an integration review that does not spend rejection rounds; an unchanged head requires ancestry on the base for `merged`. Project cleanup can remove the submitted branch after integration: ancestry of the exact reviewed head still proves the merge. A PR URL or `reference_only` records its reference without asserting local ancestry. These checks do not prove the entire resulting base tree stayed unchanged: avoiding unreviewed conflict resolutions remains part of the Worker's instructions, not a Git-write sandbox.
- Pending permission requests are read through an owner-scoped host capability and link to the existing Session approval UI; Factory neither auto-approves nor widens rules. The page refreshes on approval invalidation rather than polling.
- Startup reconciles Cards and owned groups, recovers interrupted phases with bounded continuation and archives orphan groups. Quiesce drains owned work before registration retirement. Closed done/discarded groups retain readable Sessions until Card deletion.

## Attention and page

Entering `waiting` or receiving a Proposal sends an owner-keyed transient notification; waiting Cards set the `board` page badge. Closing a group invalidates its Card again after the Run ends, so cached status and usage do not remain live without another Card mutation. The Board is the durable attention record. Generic notification validation, event routing, Settings switches and retirement behavior belong to the host/Accessor maps, not this store.

Routes are `''`, `/card/<id>` and `/setups[/<project_id>]`. The page uses shared navigation, controls, autosave and its `factory.*` catalog; owner invalidations refresh data without polling or replacing active drafts. Card detail includes review recommendations/findings, summary/checks, range, event history, usage, participant History/Run streams and normal permission-approval links.

## Agent-facing text

Before changing Factory text in `agent_text.py` or the Factory section of `vbot-docs/references/extension-usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. Worker/Reviewer instructions and continuation data are separated and labeled; continuations become canonical notes, never fabricated user instructions.

## Verification routes

- Persisted Card/setup/audit contracts: `tests/resources/extensions/factory/test_factory_store.py`.
- Public Tool origins, operator guards and scheduler admission: `tests/resources/extensions/factory/test_factory_operations.py`.
- Execution, stale roles, budgets, review/integration and restart: `tests/resources/extensions/factory/test_factory_execution.py`; Git facts: `test_factory_git.py` in the same directory.
- Rendered Board, actions and recommendations: `webui/src/components/__tests__/FactoryPage.test.js`; shared notification/bridge and host tests live with their respective owners.
- Real Model evaluation uses the development instance and a disposable registered Git Project; verify repository state and canonical participant calls, not the Models' completion claims (`vbot-agent-eval` Skill).
