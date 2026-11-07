# Software Factory

The bundled `resources/extensions/factory/` Extension carries Project work from Cards through the steps of a per-Project Workflow to the user's acceptance; it loads through ordinary bundled discovery (API 25). Factory schedules Agents and records their results. It runs no Git commands, verifies no branches or commits, and has no special integration phase: the starter Workflows express integration as an ordinary step that waits for approval.

## Terms

Shared Agent, Session, Run, Tool and Project vocabulary lives in GLOSSARY.

### Card
One unit of Factory work with an objective, Project, provenance, lifecycle and participant Sessions. Not a Session, Run or Session Goal.

### Board
The Factory page showing Cards in lifecycle columns, with `Waiting for you` as the decision inbox.

### Workflow
A Project's named list of steps and their dependencies; a setup holds several, one is the default, and a Card snapshots the one it starts with. Not a GitHub workflow or a Skill.

### Step
An Agent task within a Workflow, with dependencies on earlier steps and optional user approval or a target for returning work. Not a Model turn or Run.

### Output
A recorded step result (summary and optional document) or a request for changes with findings. Later dependent steps receive the latest results of their predecessors.

### Project setup
The Factory's Project-specific Workflows, default Workflow and execution turn budget.

### Origin
The recorded source of a Card, deciding whether it starts automatically or waits as a Proposal.

## Ownership

- `_registration.py` declares the page, public and private Tools, settings, operations and required Session runtime callbacks; import and registration acquire no resources.
- `extension.py` owns the single serialized scheduler, host lifetime, change publication and attention. `_management.py` owns operator actions and read projections. `store.py` owns domain persistence, not canonical Session storage.
- `_workflows.py` owns Workflow validation, starter templates, dependency traversal and conversion from earlier setup formats. `_execution.py` owns participant configuration, step admission, submission guards, budgets and reconciliation.
- `agent_text.py` owns Agent-facing definitions, instructions, notes and results; `_workflows.py` also holds the templates' task text. `ui/` owns page state and presentation through the generic Extension bridge; assets build to `web/` with the WebUI build.
- The existing Extension host owns user attention, current-input authority and owned approval visibility: these require current registration and canonical Run identity, not a Factory-local notification or approval store (`extensions/host.md`).

## Persistence and transitions

The host opens `ext.factory.factory`; tables are STRICT with explicit SELECT/INSERT columns and code-validated states. Setups (Workflows as JSON), Cards, Outputs, comments and audit events live here; participant history, receipts and usage remain canonical host reads. A Card change, its event and any attached comment/Output commit in one transaction. Existing SQL columns and earlier tables remain; the named `workflows` and `steps` migrations carry earlier setups forward. Output projections expose `step` and `step_name`, while stored columns retain their earlier names. Revision checks prevent stale operator drafts overwriting newer records; acceptance requires the displayed Card's `expected_revision`.

Origins are host-attributed, never a Tool argument. Operator operations queue; `factory_card` queues current human requests as `user` and currently authorized tasks as `automation`. Sub-Agents, owned participants and other initiative produce `agent` Proposals. API 24's `input_authority` reads the exact live Run; `host.review_context` includes old and ancestor requests and is unsuitable for this decision (`core/runtime/_shell_review_context.py`). Provenance stores the source Session address and Run id.

New execution uses `proposal -> queued -> working -> waiting -> done`, with discard available before completion. Steps run one at a time in list order when their dependencies are done; independent steps do not execute concurrently. `after` names earlier steps, and `returns_to` must be a direct or transitive predecessor. Results are passed through the dependency graph, not merely by list position. A step's `approval` gates its start. Waiting reasons `checkpoint`, `finished` and `no_agreement` are acceptable: Accept starts the gated step, finishes the Card, or overrules the step whose send-back limit was exhausted and continues.

Automatic changes requests rerun the target and its transitive dependents; unrelated finished steps stay done. `rounds` counts permitted automatic send-backs, not total reviews. Downstream approvals reset; the target keeps its own approval. Operator Send back can choose any step, resets send-back counts and records a comment; the default is the current completed step or latest completed step, following its return target when set. Answer resumes the blocked participant. Failures and exhausted turn budgets wait for the user. Discard cancels owned work without cleanup turns or repository changes; deletion of a final Card archives its owned group.

The scheduler uses one in-process lock, selects lowest-position enabled-Project work, and enforces `wip_limit` (default 2). Waiting does not count. `max_waiting` defaults to 0 (unlimited). Operator resumes respect WIP: accepted/retried/sent-back Cards queue their intended action when slots are full. Those Cards expose `queued_action` and cannot edit their objective while waiting. Live Extension configuration callbacks rerun admission without polling. Legacy `reviewing` and `integrating` states remain recognized for recovery, not new execution.

Cards retain their Workflow, turn budget and Project-directory snapshot, participant Run ids and delivery state. Setup edits do not change started Cards; setup deletion refuses unfinished Cards. Reconcile upgrades Cards from older role/stage execution: work without a hand-in requeues; handed-in work waits for a decision, and an earlier branch hand-in becomes an Output for the approval-gated final integration step. Missing setup information pauses as `run_failed`. A Card whose start failed before choosing a Workflow requeues on Retry or Send back.

Preparing a continuation does not consume its pending intent: repeated request callbacks return the same delivery, and acknowledgment/startup reconcile its exact canonical Session receipt before clearing it. A newer canonical root Run failing before its request callback pauses the matching handoff for explicit retry, retaining guidance; sibling, older, foreign-generation and stale-epoch completions cannot claim it. Retry authorization is consumed before admission; an already failed canonical input gets a fresh continuation on the same History, while a new failure requires another user retry.

## Execution contracts

- One execution group per Card (`group_id == id`). Ordinary steps reuse their participant Session; steps with a return target create a fresh participant per execution (`<step>-r<n>`). Each step names an Identity Agent (`agent` -> `identity_profile`) or a Project Team member (`team` -> `repository_profile`). The Profile plus explicit `project_id` propagates Project Context; cwd alone does not. Prompt inspection shares execution's resolution.
- Setup reads/previews return effective Models keyed `<workflow>/<step>`, with an empty value when unresolved; saving refuses unresolved Models and names the step. Explicit Model/effort overrides apply after profile resolution. New setups default to enabled.
- Participants receive `factory_submit_work` and `factory_report_blocked`; only a step with `returns_to` receives `factory_request_changes`. Submission records a required summary and optional `result` (up to 100000 characters); change requests record concrete findings. Factory does not validate repository facts in these results. Agent task text and Project instructions govern read-only work, isolation, checks, commits and integration; normal Permission Rules remain in force.
- `run_finished` fires before its Run releases the Session. Next-step admission runs in tracked work after release. Successful private Tools use `context.request_turn_end()`; no extra Model-facing channel is introduced. Non-submitting successful turns consume the per-step budget (default 30).
- Temporary groups refuse participant creation while open. Fresh admission drains/closes/reopens through the existing owner API; obsolete participants and late callbacks cannot move a newer step.
- Pending permission requests use owner-scoped host reads and link to the existing Session approval UI; Factory neither auto-approves nor widens rules. The page refreshes on approval invalidation, not polling.
- Startup reconciles Cards and groups with bounded recovery and archives orphan groups. Quiesce drains work before registration retirement. Closed done/discarded groups retain readable Sessions until Card deletion.

## Attention and page

Entering `waiting` or receiving a Proposal sends an owner-keyed transient notification; both count toward the `board` page badge. Missing question/note text falls back to a readable Board-directed sentence. Closing a group invalidates its Card after the Run ends, so status and usage do not remain live. The Board is the durable attention record; generic notification routing and retirement belong to the host/Accessor maps.

Routes are `''`, `/card/<id>` and `/setups[/<project_id>]`. The page uses shared navigation, controls, autosave and its `factory.*` catalog; invalidations preserve active drafts. Setup editing and Card progress share `WorkflowDiagram.svelte`: solid arrows show dependencies, dashed arrows show return targets and their limits, and approval badges mark gated steps. Clicking a setup node opens its task, Agent/Model/effort and dependency editor. Reordering/removing dependencies repairs invalid return targets, including downstream ones.

Card detail shows progress from `done_steps` and the current step, summaries/documents/findings from Outputs, event history, usage, participant History/Run streams and permission links. Earlier documents fold while the latest document per step stays open. Send back offers every step. There are no special branch, commit-range or integration displays or setup fields.

## Agent-facing text

Before changing Factory text in `agent_text.py`, `_workflows.py` or the Factory section of `vbot-docs/references/extension-usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. System rules, the step's task and continuation data remain separated and labeled; continuations become canonical notes, never fabricated user instructions.

## Verification routes

- Persisted Card/setup/audit contracts and Workflow validation: `tests/resources/extensions/factory/test_factory_store.py`.
- Public Tool origins, operator guards and scheduler admission: `tests/resources/extensions/factory/test_factory_operations.py`.
- Step execution, rework, approval gates, stale participants, budgets and restart: `tests/resources/extensions/factory/test_factory_execution.py`.
- Rendered Board, diagram, actions, results and autosave: `webui/src/components/__tests__/FactoryPage.test.js`; shared notification/bridge and host tests live with their owners.
- Real Model evaluation uses the development instance and a disposable registered Project; verify repository state and canonical participant calls, not completion claims (`vbot-agent-eval` Skill).

## References

Read these only when your task matches - not by default.

- Changing Factory Agent-facing text -> `extensions/factory-agent-facing-text.md`
