# Software Factory

The bundled `resources/extensions/factory/` Extension carries work as Cards through the steps of global Workflows to the user's acceptance; it loads through ordinary bundled discovery. Factory schedules Agents, starts Workflows from triggers and records results. It runs no Git commands, verifies no branches or commits, and has no special integration phase: the starter Workflows express integration as an ordinary step that waits for approval.

User decisions 2026-11-14: Workflows are global with an optional Project (fixed, or derived from a trigger such as a GitHub repository); triggers belong to Workflows and use the Cron trigger mechanics; steps form a graph with parallel steps, send-back, approval halts and steps that start another Workflow; waiting for the user is set per step (`approval`) and per Workflow (`final_approval`); the shared template Library and per-Project setups are removed.

## Terms

Shared Agent, Session, Run, Tool and Project vocabulary lives in GLOSSARY.

### Card
One unit of Factory work with an objective, an optional Project, provenance, lifecycle and participant Sessions. Not a Session, Run or Session Goal.

### Board
The Factory page showing Cards in lifecycle columns, with `Waiting for you` as the decision inbox.

### Workflow
A global, named graph of steps with optional Project, triggers, limits and final approval; a Card snapshots the Workflow it starts with. Not a GitHub workflow or a Skill.

### Step
A node of a Workflow: an Agent task (`agent`) or the start of another Workflow as a child Card (`workflow`), starting once the steps in its `after` are done. Not a Model turn or Run.

### Trigger
A Workflow's schedule or webhook that creates Cards for it by itself. Not a Cron job the user manages; Cron owns its mechanics (`automation/cron.md`, `extensions/host.md`).

### Output
A recorded step result (summary and optional document) or a request for changes with findings. Later dependent steps receive the latest results of their predecessors.

### Origin
The recorded source of a Card (`operator`, `user`, `automation`, `agent`, `trigger`, `workflow`), deciding whether it starts automatically or waits as a Proposal.

## Ownership

- `_registration.py` declares the page, public and private Tools, settings, operations (with `workflow.secret` as a secret operation) and required Session runtime callbacks; import and registration acquire no resources.
- `extension.py` owns the single serialized scheduler, Card creation (operator, trigger and child Cards), Project resolution, host lifetime, change publication and attention. `_management.py` owns operator actions and read projections. `store.py` owns domain persistence, not canonical Session storage.
- `_workflows.py` owns Workflow validation, starter templates and graph navigation (ancestors, dependents, sinks, group ids). `_execution.py` owns per-step participant configuration, admission, submission guards, child Cards, budgets and reconciliation. `_triggers.py` owns syncing Workflow triggers to host triggers, webhook condensing, repository-to-Project matching and fires.
- `agent_text.py` owns Agent-facing definitions, instructions, notes and results; `_workflows.py` also holds the templates' task text. `ui/` owns page state and presentation through the generic Extension bridge; assets build to `web/` with the WebUI build.
- The Extension host owns user attention, current-input authority, owned approval visibility and Extension triggers (schedule, webhook URL, signature secret, catch-up): Factory never stores schedules or secrets itself (`extensions/host.md`).

## Persistence and transitions

The host opens `ext.factory.factory`; tables are STRICT with explicit SELECT/INSERT columns and code-validated states. `workflows` holds the global Workflows (definition JSON, position, revision); Cards carry `workflow_id` and `parent_id`; `card_triggers` keeps the condensed event that created a triggered Card. Outputs, comments and audit events live alongside; participant history, receipts and usage remain canonical host reads. A Card change, its event and any attached comment/Output commit in one transaction. Tables evolve additively: the old `setups` and Library tables stay on disk and inert. The `global_workflows` migration copies each setup's Workflows to global ones with that Project fixed and `final_approval` on, keeping ids unless taken, and moves started unfinished Cards to the per-step runtime; a step that was in progress waits as `upgraded` for Retry. Output rows store the step in `stage_id`/`stage_name`; projections expose `step` and `step_name`. Revision checks prevent stale drafts overwriting newer records; acceptance requires the displayed Card's `expected_revision`.

Origins are host-attributed, never a Tool argument. Operator operations queue; `factory_card` queues current human requests as `user` and currently authorized tasks as `automation`. Sub-Agents, owned participants and other initiative produce `agent` Proposals. Trigger and child Cards queue as `trigger` and `workflow`. API 24's `input_authority` reads the exact live Run; `host.review_context` includes old and ancestor requests and is unsuitable for this decision (`core/runtime/_shell_review_context.py`). Provenance stores the source Session address and Run id.

Cards move `proposal -> queued -> working -> waiting -> done`, with discard available before completion. A started Card's runtime (version 2) holds its Workflow snapshot, working folder and one state per step: `pending`, `running`, `child`, `waiting` (with `reason`), `approval` or `done`. Card state and `waiting_reason` derive from the steps after every transition: a runtime `blocker` first (`workflow_missing`, `project_unresolved`, `run_failed` before any step ran), then the first step in `approval` (`checkpoint`), then the first `waiting` step's reason, then `finished` when all are done under `final_approval`; otherwise done or working. Parallel steps keep running while another step waits.

Every step whose `after` steps are done starts, so independent steps run concurrently. `returns_to` must be a direct or transitive predecessor. Results pass through the dependency graph. A step's `approval` gates its start. Step actions take an optional `step` (default: the first matching waiting step): Accept approves a `checkpoint` or overrules `no_agreement`; without a step it accepts a `finished` Card. Answer resumes a `blocked` participant. Retry resumes `run_failed`, `restart`, `budget_exhausted`, `upgraded` or `child_failed`, and re-queues a Card with a blocker.

Automatic change requests rerun the target and its transitive dependents; unrelated finished steps stay done. `rounds` counts permitted automatic send-backs, not total reviews. Downstream approvals reset; the target keeps its own approval. Operator Send back can choose any step and records a comment; the default is the first waiting step's return target (or that step), else the last sink's. Send back is refused while a blocker holds the Card. Discard cancels owned work and discards waiting child Cards, without cleanup turns or repository changes; deletion of a final Card archives its owned groups.

The scheduler uses one in-process lock, starts queued Cards in Board order and enforces `wip_limit` (default 2) over Cards with a `running` Agent step; waiting Cards and Cards waiting only on a child do not count. `max_waiting` defaults to 0 (unlimited). Operator resumes respect WIP and expose `queued_action`; acceptance that finishes a Card needs no slot. Live Extension configuration callbacks rerun admission without polling.

Admitted operator Card and Workflow mutations retain the service lock through persistence, publication and launch scheduling even when their caller is cancelled. Cancellation while waiting for the lock starts no mutation.

## Workflows, Projects and child Cards

- `card.create` and `factory_card` resolve `workflow` by exact id, then name, among enabled Workflows; omitted means the first enabled one. A Workflow with `project_id` always works there; a different requested Project is refused (`project_mismatch`). A Card may have no Project.
- At start, the Workflow must still exist (else blocker `workflow_missing`). The Project is the Workflow's, else the Card's, else (for a `@repository` trigger) the Project whose Git remote matches the event's repository (else blocker `project_unresolved`). A Card without a Project works in `<state_dir>/workspaces/<card id>`; Team (`source: team`) steps then refuse to start; the page refuses to save Team steps in a Workflow without Project.
- A `workflow` step creates a child Card (origin `workflow`, `parent_id` set) for the named Workflow, under the child Workflow's Project or the parent's, with the step task, the parent objective and the latest predecessor results as description. With `wait` the step is `child` until the child ends: done with the child's sink results as Output, or `child_failed` when it was discarded. Without `wait` it is done once the child exists. Nesting stops at `MAX_DEPTH` 5 (`too_deep`).
- Workflow edits never change started Cards. Deleting a Workflow is refused while another Workflow starts it or while proposal/queued Cards wait for it (`workflow_in_use`); its triggers are removed.

## Triggers

Each Workflow trigger maps to one host Extension trigger keyed `<workflow id>:<trigger id>`; `_triggers.sync` reconciles them after every Workflow save, delete and at start, and `workflow.list`/`get` include each trigger's host status (next/last fire, last error, webhook URL and path). Schedules accept what `parse_owned_schedule` accepts (cron, `every <duration>`, `in <duration>`, ISO instant; `automation/cron.md`). `workflow.secret` reads or rotates a webhook's signing secret through the host; secrets never enter Factory records or Workflow definitions.

A fire creates Cards only for an enabled Workflow: one per scheduled fire (with missed-fire counts), or one per webhook request, newest 20 per fire with a skipped count. Webhook bodies are condensed (event, repository, ref, pusher, up to 20 commits, bounded text) and stored per Card; the participant receives it as `Event that created the Card`. Trigger `project` is empty, a Project id, or `@repository` (webhooks only). A trigger naming a deleted Project creates a Card without Project that waits as `project_unresolved`. GitHub webhooks need the vBot address reachable from GitHub; no email or other event sources exist yet.

## Execution contracts

- Each step has its own execution group `<card>.<step>`; participants reuse their Session across reruns, while steps with a return target create a fresh participant per execution (`<step>-r<n>`). Each step names an Identity Agent (`agent` -> `identity_profile`) or a Project Team member (`team` -> `repository_profile`). The Profile plus explicit `project_id` propagates Project Context; cwd alone does not. Prompt inspection shares execution's resolution.
- Workflow reads/previews return effective Models keyed `<workflow>/<step>`, with an empty value when unresolved; saving refuses unresolved Models and names the step. Explicit Model/effort overrides apply after profile resolution.
- Participants receive `factory_submit_work` and `factory_report_blocked`; only a step with `returns_to` receives `factory_request_changes`. Submission records a required summary and optional `result` (up to 100000 characters); change requests record concrete findings. Factory does not validate repository facts in these results. Agent task text and Project instructions govern read-only work, isolation, checks, commits and integration; normal Permission Rules remain in force.
- `run_finished` fires before its Run releases the Session. Next-step admission runs in tracked work after release. Successful private Tools use `context.request_turn_end()`; no extra Model-facing channel is introduced.
- Temporary groups refuse participant creation while open. Fresh admission drains/closes/reopens through the existing owner API; obsolete participants and late callbacks cannot move a newer step.
- Preparing a continuation does not consume its pending intent: repeated request callbacks return the same delivery, and acknowledgment/startup reconcile its exact canonical Session receipt before clearing it. A newer canonical root Run failing before its request callback pauses that step for explicit retry. Retry authorization is consumed before admission.
- Pending permission requests use owner-scoped host reads and link to the existing Session approval UI; Factory neither auto-approves nor widens rules. The page refreshes on approval invalidation, not polling.
- Startup reconciles Cards and groups with bounded recovery and archives orphan groups. Quiesce drains work before registration retirement. Closed done/discarded groups retain readable Sessions until Card deletion.

## Execution limits

User decision 2026-11-08: optional independent limits for Runs, admitted Tool calls, input plus output Tokens, and Model cost in USD. Workflow `limits` apply to the entire Card; each Agent step's `limits` apply to that step execution. Omitted or zero values are unlimited. A new step execution or rework resets its step counters; Card totals persist. Pause, Answer, Retry and restart preserve consumption. The first exhausted cap pauses that step as `budget_exhausted`; the step state's `limit_hit` identifies scope, metric, used, limit and reason.

The service lock serializes Run admission and durable Tool reservations. The required owner Tool callback covers nested programmatic calls and owned Sub-Agents before any effect; refused calls do not consume capacity. Root admissions and observed child Runs are deduplicated. Tokens/cost include auxiliary requests and estimates from settled canonical Usage at stored prices; a running request can overshoot, and unavailable required usage pauses rather than counting as free. Required admission also runs before each physical Chat request, after automatic Compaction and retry waiting.

At the next root participant Model request, a canonical Note warns around 80% consumption (at least one unit before integer limits), naming scope, usage, cap and remaining capacity, and asks for completion or a blocked hand-in with progress, remaining work and needed budget. Per-recipient receipt acknowledgement prevents repeats across Retry/restart; raised caps and new step executions have their own warning. No standing System Prompt rule is added.

The Workflow editor configures Workflow and step limits. Card detail shows limits and consumption per step (`card.get` `budget` keyed by step id); a waiting Card changes its snapshot through revision-checked `card.limits` (`card_limits`, and `step_limits` with `step`). Empty objects clear a scope. `_limits.py` owns normalization, consumption interpretation and thresholds; `_execution.py` owns reservation and delivery under its lock.

## Attention and page

Entering `waiting` or receiving a Proposal sends an owner-keyed transient notification; both count toward the `board` page badge. Missing question/note text falls back to a readable Board-directed sentence. The Board is the durable attention record; generic notification routing belongs to the host/Accessor maps.

Routes are `''`, `/card/<id>` and `/workflows[/<id>|/new]`; invalidation resource `workflows` reloads the list and the open Workflow, and a Workflow deleted elsewhere returns to `/workflows`. The page uses shared navigation, controls, autosave and its `factory.*` catalog (English only); invalidations preserve active drafts. A saved Workflow autosaves; a new one, from a blank start, a template or a copy, is created with an explicit button. The Workflow editor and Card progress share `WorkflowDiagram.svelte`: columns follow dependencies, dashed arrows show return targets, approval badges mark gated steps. Reordering/removing dependencies repairs invalid return targets, including downstream ones.

Card detail shows each waiting step with its own Accept/Answer/Retry, Card-level actions, the step diagram with live statuses, Outputs (earlier documents fold), child and parent Cards, the trigger event, events, usage, per-step limits, participant History/Run streams (group `<card>.<step>`) and permission links.

## Agent-facing text

Before changing Factory text in `agent_text.py`, `_workflows.py`, the operation descriptions in `_values.py` or `resources/extensions/factory/docs/usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. System rules, the step's task and continuation data remain separated and labeled; continuations become canonical notes, never fabricated user instructions.

## Verification routes

- Persisted Workflows, Cards, triggers' events and the setup migration: `tests/resources/extensions/factory/test_factory_store.py`.
- Public Tool origins, operator guards, Workflow management and scheduler admission: `test_factory_operations.py` in the same directory.
- Step graph execution, parallel steps, rework, approval gates, child Cards, stale participants, budgets and restart: `test_factory_execution.py`.
- Trigger sync, fires, webhook condensing and repository matching: `test_factory_triggers.py`; host trigger contracts: `tests/core/automation/test_cron_owned.py` and `tests/core/runtime/test_runtime_extension_host_triggers.py`.
- Rendered Board, Workflow editor, triggers, card decisions, limits and autosave: `webui/src/components/__tests__/FactoryPage.test.js`.
- Real Model evaluation uses the development instance and a disposable registered Project; verify repository state and canonical participant calls, not completion claims (`vbot-agent-eval` Skill).

## References

Read these only when your task matches - not by default.

- Changing Factory Agent-facing text -> `extensions/factory-agent-facing-text.md`
