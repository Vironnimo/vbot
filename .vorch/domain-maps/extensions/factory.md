# Software Factory

The bundled `resources/extensions/factory/` Extension owns permanent Project Factories with teams, Kanban tasks and shared discussions. Native work requires no Workflow or repository-scanned Project Team. The same Extension retains independent global Workflows and their execution Cards; it loads through ordinary bundled discovery and uses canonical host Sessions and Runs.

User decisions 2026-11-16: each Project Factory has a persistent Leader and members; members consult peers directly and the user can join any discussion. Assignment starts eligible work. Tasks may have separately assigned children and dependencies; the responsible owner verifies the integrated result. Owner completion is the default, with independent review or user approval selected per task. The existing scanned Project Team remains a separate compatibility feature.

User decisions 2026-11-14: Workflows are global with an optional Project (fixed, or derived from a trigger such as a GitHub repository); triggers belong to Workflows and use the Cron trigger mechanics; steps form a graph with parallel steps, send-back, approval halts and steps that start another Workflow; waiting for the user is set per step (`approval`) and per Workflow (`final_approval`); the shared template Library and per-Project setups are removed.

## Terms

Shared Agent, Session, Run, Tool and Project vocabulary lives in GLOSSARY.

### Card
One execution of a global Workflow, with an objective, an optional Project, provenance, lifecycle and participant Sessions. Not a native Factory Task, Session, Run or Session Goal.

### Board
The Factory view showing native Tasks in Kanban columns. The separate Workflow Board shows execution Cards and their decision inbox.

### Factory
A permanent working environment for a registered Project, with a Leader, members, tasks and discussions. Not a Workflow or the repository-scanned Project Team.

### Factory Team
The persistent Identity Agent memberships of one Factory, including its Leader. Not the scanned Project Team defined in GLOSSARY, and not a set of temporary Sub-Agents.

### Factory Task
A unit of native work with an objective, completion conditions and a responsible member, optionally organized into a hierarchy with dependencies. Not a Workflow Card or Session Goal.

### Topic
A shared Factory discussion with attributed posts and invited members, optionally linked to a task. Not a multi-Agent Session; each responding member uses its own canonical Session.

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
- `extension.py` owns the single serialized scheduler, Card creation (operator, trigger and child Cards), Project resolution, host lifetime, change publication and attention. `_management.py` dispatches operator actions and read projections. `store.py` owns Workflow persistence and composes the native store; neither owns canonical Session storage.
- `_native_store.py` composes native persistence through `_native_records.py`, `_native_tasks.py`, `_native_topics.py`, `_native_dispatch.py` and `_native_schema.py`. These internals own permanent records, revisions, hierarchy, discussions and durable dispatch intentions. `_native_management.py` and `_native_values.py` own native operations and binding-authenticated Agent actions; `_native_identities.py` owns creation receipts and Model preflight shared by Factory bootstrap and membership. `_native_execution.py` owns native coordination and scheduling, `_native_sessions.py` its canonical admission/delivery/drain lifecycle; `_native_discussions.py` and `_native_workflows.py` own peer delivery and Workflow association under the same service lock. `_native_recovery.py` derives interrupted transitions from persisted records; `_native_handoff.py` drains predecessors and captures bounded canonical progress. `_native_limits.py` checks native allocations using `_budget_index.py` and `_budget_store.py` (incremental budget projections); `_native_policy.py` enforces scoped admission.
- `_workflows.py` owns Workflow validation, starter templates and graph navigation (ancestors, dependents, sinks, group ids). `_execution.py` owns Workflow graph transitions, submission guards, child Cards and operator decisions; `_workflow_sessions.py` owns participant configuration, canonical admission/delivery, draining and reconciliation; `_workflow_budgets.py` owns Workflow counters, limit checks and receipt-backed warnings. `_triggers.py` owns syncing Workflow triggers to host triggers, webhook condensing, repository-to-Project matching and fires.
- `agent_text.py` and `_native_agent_text.py` own Agent-facing definitions, instructions, notes and results; `_workflows.py` also holds the templates' task text. `ui/` owns page state and presentation through the generic Extension bridge; `NativeActions.svelte` owns action drafts/dialogs and `CardLimits.svelte` the operational Card/step budget draft; assets build to `web/` with the WebUI build.
- The Extension host owns user attention, current-input authority, owned approval visibility and Extension trigger execution (resolved schedule, webhook URL, signature secret, catch-up); Factory stores trigger definitions but no scheduling state or secrets (`extensions/host.md`).

## Native Factories and membership

`FactoryStore.native` uses additive `native_*` tables in the existing `ext.factory.factory` database. One Factory belongs to a registered Project and retains a Leader membership, remit, capacity, configuration ceiling and optional resource limits. A member references an actual Identity Agent and its persisted incarnation proof, with local role, instructions, Model/effort/Tool/Skill choices and capacity. Retiring membership preserves its records and history. It does not delete the Agent or reinterpret the scanned Project Team.

Native Sessions use API 32's `identity_agent` and `identity_incarnation` owned-execution mode. The host retains the Agent's Workspace, personal Memory, private Skills and SOUL, while attaching a separate Working Project and Session-scoped instructions/private Tools. Ordinary Sessions keep their original capabilities. The host refuses replaced Agent incarnations; a legacy identity without creation evidence receives a durable proof before attachment. Native code uses host interfaces, never direct Session database access.

The operator can reuse an existing Leader or create one, then add existing Agents or create permanent specialists. Creation uses a durable Factory reservation plus the host's owner-bound creation request to reconcile interruption across the two stores. Member creation validates the Model ceiling before reserving or creating an identity. The same exact request returns its original identity; a different payload or replacement incarnation is refused. Shared Agents receive local configuration; explicit global updates are restricted to identities created by this Extension. Membership settings narrow the operator's Factory boundary and the Agent's own capability policy.

Member-local Model and effort choices override Factory defaults, which override the identity baseline. Explicit `clear_fields` removes known local/Factory overrides while retaining unrelated persisted JSON. Model selection must resolve to an available Chat Model within the configured ceiling; physical admission checks the actual selected Model/Connection, including fallback. Tool admission also checks descendants, and generic owned execution bounds their Skill catalog and reads through the same owned Skill ceiling.

Native operation families are `factory.*`, `member.*`, `task.*` and `topic.*`; `_native_values.py` is their exact parameter contract. Browser and CLI operations use the same management owner. `factory_settings`, `factory_members`, `factory_tasks` and `factory_topics` are private Session Tools. Tool actors derive from the exact owned binding, participant, generation, member revision, task epoch and Run. Caller-provided ids do not confer authority. Leader administration, task ownership and configured review rights are checked by the service as well as reflected in Tool visibility. Factory lifecycle, remit, capacity, ceilings and budgets remain operator-only; the Leader can edit its name. The trusted local operator CLI remains host access, not an OS sandbox.

## Native tasks and completion

Native Tasks are separate from Workflow Cards; `cards.parent_id` keeps its nested-Workflow meaning. Assigning a native task queues eligible work automatically with no Workflow or Goal requirement. Task states are `backlog`, `ready`, `working`, `blocked`, `paused`, `review`, `done` and `cancelled`; blocked work carries a reason. Dependencies determine readiness. Parents retain their own owner and result; child completion wakes the owner but never completes the parent implicitly.

Hierarchy and dependency references must stay within one Factory, and their combined completion graph must be acyclic. A required unfinished child or prerequisite prevents submission. Adding a new child to a completed/submitted parent is refused until the parent is reopened. Reopening a prerequisite invalidates unfinished dependent claims while preserving explicit pauses.

Task writes validate the displayed revision or execution epoch and record attributed events. Reassignment, agreement changes and explicit stop actions fence obsolete execution before draining its group. A successor waits for the predecessor and its descendants to drain; the draining execution retains its capacity reservation. It then receives bounded, attributed canonical progress from the prior generation, while complete history remains inspectable. Old results remain in history. A member submits only from its current assigned task execution; it cannot relax that task's agreed completion or review conditions. Other members contribute through discussions or propose unassigned work. Owners can create and assign children within their remit; the Leader organizes work across the Factory.

Completion requires an outcome and evidence. Without configured gates it completes the task. A reviewer Run ending without a decision blocks as `review_input`; explicit `task.resume` continues review with the submitted agreement and separate approvals retained, including after failure or restart. A selected reviewer and user approval compose as separate approvals tied to the submitted agreement; requesting changes advances the epoch and returns work to its owner. The system verifies ownership, evidence presence and lifecycle prerequisites, not the semantic truth of a result or repository correctness.

## Native conversations, admission and recovery

Topics hold attributed posts and same-Factory participants independently of canonical execution transcripts. Any active member can open a topic and invite peers; the user can post throughout. Invitations, user messages and explicitly addressed member messages schedule bounded context into one canonical Session per topic/member. Linked consultation answers and user information reach the responsible task Session without a Leader relay or a duplicate responding topic Session. An unaddressed member reply is recorded without broadcasting a new automatic response cycle.

A newly invited participant receives recent shared history, including earlier unaddressed answers, rather than only posts addressed to that new participant. A bounded recent-post store query supplies this context; delivery/read cursors still advance exactly once. Subsequent wake targeting remains addressed so a shared history excerpt does not create a reply loop.

Read markers and task wake intentions are persisted together. Exact canonical input/delivery receipts reconcile interruption and deduplicate delivery. Continuations are attributed notes, not invented human authorization. A paused task or Factory retains unread input and needs explicit resume; the first resumed request includes it. A closed topic stops automatic replies. Failed Runs remain visibly blocked with retained progress instead of replaying effects as new work.

One service lock serializes native/Workflow admission and budget reservations. Global WIP, Factory capacity and member capacity constrain eligible work; idle discussions and blocked tasks consume no running slot. Stored Run/Tool reservations and canonical settled Usage cover task executions, peer consultation, review, descendants and associated Workflow Cards. Optional Run, Tool-call, token and USD limits aggregate through ancestor allocations and the Factory; pause, reassignment, retry and restart retain consumption. Uncapped admission reads only the current task ancestor chain before skipping Factory-wide consumption scans. Capped admission follows coalesced transactional domain revisions and API 34 Usage group revisions; it updates changed contributions and their ancestor allocations rather than rereading retained Factory history. Task reparenting transfers retained spending in the disposable index; cold start or either database restore rebuilds from source. The `factory_budget_changes` migration seeds prior records, while insert/update/delete triggers keep older writers visible. No callbacks or wall-clock expiry decide cache validity. Missing required Usage blocks admission, and an already admitted Model request can exceed a remaining token/cost allowance.

Supplying a native limits map replaces the known caps: zero removes the selected cap and an empty map clears known caps, without resetting consumption or deleting unknown future fields. Factory/member policy edits fence affected nonterminal executions, including paused/blocked work; explicit pauses remain paused until resumed with current scoped configuration.

Owned native groups participate in startup reconciliation, shutdown and archive alongside Workflow groups. Recovery derives stale scope repairs, archive cleanup and child/Workflow completion wakes from committed records, even when a process stopped before its change callback. Wake/read markers commit together so repeated reconciliation does not repeat delivery. Closing/draining happens outside the service lock. Before archival, drained task progress is cached against its exact execution, binding, generation and Run; successors can reuse that bounded canonical excerpt after member retirement and restart. Factory archive stops admission, retains history and archives owned groups so Project lifecycle operations can proceed. Optional Workflow selection creates an associated existing-style Card; its outcome resumes the native owner for integrated completion, while task scope and limits continue to govern the association.

Associated no-wait child Cards can continue after their root Workflow completes. Task pause drains unfinished associated work; resume reconciles its policy without repeating the completed root. Task completion/cancellation and reassignment fence unfinished descendants. A stable result marker deduplicates Workflow evidence independently of authority-only Card revisions. Linked Cards and their descendants retain results and budget attribution until Factory archive; deletion is refused before any mutation while the Factory is live. Legacy missing references detach safely and block affected work as `workflow_missing` for explicit resume; they never abort recovery or unrelated admission.

## Workflow persistence and transitions

The host opens `ext.factory.factory`; tables are STRICT with explicit SELECT/INSERT columns and code-validated states. `workflows` holds the global Workflows (definition JSON, position, revision); Cards carry `workflow_id` and `parent_id`; `card_triggers` keeps the condensed event that created a triggered Card. `trigger_receipts` deduplicates scheduled fires and webhook requests in the Card-creation transaction, retaining receipts after Card deletion. Outputs, comments and audit events live alongside; participant history, Session delivery receipts and usage remain canonical host reads. A Card change, its event and any attached comment and list of Outputs commit in one transaction. Tables evolve additively: the old `setups` and Library tables stay on disk and inert. The `global_workflows` migration copies each setup's Workflows to global ones with that Project fixed and `final_approval` on, keeping ids unless taken, and moves started unfinished Cards to the per-step runtime; a step that was in progress waits as `upgraded` for Retry. Output rows store the step in `stage_id`/`stage_name`; projections expose `step` and `step_name`. Revision checks prevent stale drafts overwriting newer records; acceptance requires the displayed Card's `expected_revision`.

Origins are host-attributed, never a Tool argument. Operator operations queue; `factory_card` queues current human requests as `user` and currently authorized tasks as `automation`. Sub-Agents, owned participants and other initiative produce `agent` Proposals. Trigger and child Cards queue as `trigger` and `workflow`. API 24's `input_authority` reads the exact live Run; `host.review_context` includes old and ancestor requests and is unsuitable for this decision (`core/runtime/_shell_review_context.py`). Provenance stores the source Session address and Run id.

Cards move `proposal -> queued -> working -> waiting -> done`, with discard available before completion. A started Card's runtime (version 2) holds its Workflow snapshot, working folder and one state per step: `pending`, `running`, `child`, `waiting` (with `reason`), `approval` or `done`. Card state and `waiting_reason` derive from the steps after every transition: a runtime `blocker` first (`workflow_missing`, `project_unresolved`, `run_failed` before any step ran), then `queued_action` waiting for WIP, then the first step in `approval` (`checkpoint`), then the first `waiting` step's reason, then `finished` when all are done under `final_approval`; otherwise done or working. Parallel steps keep running while another step waits.

Every step whose `after` steps are done starts, so independent steps run concurrently. `returns_to` must be a direct or transitive predecessor. Results pass through the dependency graph. A step's `approval` gates its start. Step actions take an optional `step` (default: the first matching waiting step): Accept approves a `checkpoint` or overrules `no_agreement`; without a step it accepts a `finished` Card. Answer resumes a `blocked` participant. Retry resumes `run_failed`, `restart`, `budget_exhausted`, `upgraded` or `child_failed`, and re-queues a Card with a blocker.

Automatic change requests rerun the target and its transitive dependents; unrelated finished steps stay done. `rounds` counts permitted automatic send-backs, not total reviews. Downstream approvals reset; the target keeps its own approval. Operator Send back can choose any step and records a comment; the default is the first waiting step's return target (or that step), else the last sink's. Send back is refused while a blocker holds the Card. Discard cancels owned work and discards waiting child Cards, without cleanup turns or repository changes; deletion of a final Card archives its owned groups.

The scheduler uses one in-process lock, starts queued Cards in Board order and enforces `wip_limit` (default 2) over unfinished Cards with an admitted `running` Agent step; a waiting Card still counts if a parallel Agent step runs, while final Cards and Cards waiting only on a child do not count. `max_waiting` defaults to 0 (unlimited). Operator resumes and continuations after child completion respect WIP: `queued_action` retains the prepared inputs, counters and Workflow snapshot until admission, including across restart. These Cards cannot be edited as unstarted work, admit no Model or Tool requests, and project prepared Agent steps as pending. Acceptance that finishes a Card needs no slot. Live Extension configuration callbacks rerun admission without polling.

Admitted operator Card and Workflow mutations retain the service lock through persistence, publication and launch scheduling even when their caller is cancelled. Cancellation while waiting for the lock starts no mutation.

## Workflows, Projects and child Cards

- `card.create` and `factory_card` resolve `workflow` by exact id, then name, among enabled Workflows; omitted means the first enabled one. A Workflow with `project_id` always works there; a different requested Project is refused (`project_mismatch`). A Card may have no Project.
- At start, the Workflow must still exist (else blocker `workflow_missing`). The Project is the Workflow's, else the Card's, else the trigger's retained Project choice. A deleted explicit Project, or `@repository` without a repository or a matching Project Git remote, blocks as `project_unresolved`. Git worktrees resolve remotes through their common Git directory. The resolved Project reaches the first Agent input and child Cards. A Card without a Project works in `<state_dir>/workspaces/<card id>`; Team (`source: team`) steps then refuse to start; the page refuses to save Team steps in a Workflow without Project.
- A `workflow` step creates a child Card (origin `workflow`, `parent_id` set) for the named Workflow, under the child Workflow's Project or the parent's, with the step task, the parent objective and the latest predecessor results as description. With `wait` the step is `child` until the child ends: done with the child's sink results as Output, or `child_failed` when it was discarded. Without `wait` it is done once the child exists. Nesting stops at `MAX_DEPTH` 5 (`too_deep`).
- Workflow edits never change started Cards. Deleting a Workflow is refused while another Workflow starts it or while proposal/queued Cards wait for it (`workflow_in_use`); its triggers are removed.

## Triggers

Each Workflow trigger maps to one host Extension trigger keyed `<workflow id>:<trigger id>`; `_triggers.sync` reconciles them after every Workflow save, delete and at start, and `workflow.list`/`get` include each trigger's host status (next/last fire, last error, webhook URL and path). Schedules accept what `parse_owned_schedule` accepts (cron, `every <duration>`, `in <duration>`, ISO instant; `automation/cron.md`). `workflow.secret` reads or rotates a webhook's signing secret through the host; secrets never enter Factory records or Workflow definitions.

Reconciliation preserves relative schedule timing and terminal one-shots; changing a completed/missed trigger's schedule creates a new host trigger. A failed trigger stays stopped across saves and reloads; explicitly pause and enable it to retry.

A fire creates Cards only for an enabled Workflow and trigger: one per scheduled fire (with missed-fire counts), or one per webhook request, newest 20 per fire with a skipped count. Replayed fires skip committed receipts, so a partly failed batch does not duplicate Cards. Webhook bodies are condensed (event, repository, ref, pusher, up to 20 commits, bounded text) and stored per Card; the participant receives it as `Event that created the Card`. Trigger `project` is empty, a Project id, or `@repository` (webhooks only). A trigger naming a deleted Project creates a Card without Project that waits as `project_unresolved`. GitHub webhooks need the vBot address reachable from GitHub; no email or other event sources exist yet.

## Workflow execution contracts

- Each step has its own execution group `<card>.<step>`; participants reuse their Session across reruns, while steps with a return target create a fresh participant per execution (`<step>-r<n>`). Each step names an Identity Agent (`agent` -> `identity_profile`) or a Project Team member (`team` -> `repository_profile`). The Profile plus explicit `project_id` propagates Project Context; cwd alone does not. Prompt inspection shares execution's resolution.
- Workflow reads/previews return effective Models keyed `<workflow>/<step>`, with an empty value when unresolved; saving refuses unresolved Models and names the step. Explicit Model/effort overrides apply after profile resolution.
- Participants receive `factory_submit_work` and `factory_report_blocked`; only a step with `returns_to` receives `factory_request_changes`. Submission records a required summary and optional `result` (up to 100000 characters); change requests record concrete findings. Factory does not validate repository facts in these results. Agent task text and Project instructions govern read-only work, isolation, checks, commits and integration; normal Permission Rules remain in force.
- `run_finished` fires before its Run releases the Session. Next-step admission runs in tracked work after release. Successful private Tools use `context.request_turn_end()`; no extra Model-facing channel is introduced.
- Temporary groups refuse participant creation while open. Fresh admission drains/closes/reopens through the existing owner API; obsolete participants and late callbacks cannot move a newer step.
- Preparing a continuation does not consume its pending intent: repeated request callbacks return the same delivery, and acknowledgment/startup reconcile its exact canonical Session receipt before clearing it. A newer canonical root Run failing before its request callback pauses that step for explicit retry. Retry authorization is consumed before admission.
- Pending permission requests use owner-scoped host reads and link to the existing Session approval UI; Factory neither auto-approves nor widens rules. The page refreshes on approval invalidation, not polling.
- Startup reconciles Cards and groups with bounded recovery and archives orphan groups. Quiesce drains work before registration retirement. Closed done/discarded groups retain readable Sessions until Card deletion.

## Workflow execution limits

User decision 2026-11-08: optional independent limits for Runs, admitted Tool calls, input plus output Tokens, and Model cost in USD. Workflow `limits` apply to the entire Card; each Agent step's `limits` apply to that step execution. Omitted or zero values are unlimited. A new step execution or rework resets its step counters; Card totals persist. Pause, Answer, Retry and restart preserve consumption. The first exhausted cap pauses that step as `budget_exhausted`; the step state's `limit_hit` identifies scope, metric, used, limit and reason.

The service lock serializes Run admission and durable Tool reservations. The required owner Tool callback covers nested programmatic calls and owned Sub-Agents before any effect; refused calls do not consume capacity. Root admissions and observed child Runs are deduplicated. Tokens/cost include auxiliary requests and estimates from settled canonical Usage at stored prices; a running request can overshoot, and unavailable required usage pauses rather than counting as free. Required admission also runs before each physical Chat request, after automatic Compaction and retry waiting.

At the next root participant Model request, a canonical Note warns around 80% consumption (at least one unit before integer limits), naming scope, usage, cap and remaining capacity, and asks for completion or a blocked hand-in with progress, remaining work and needed budget. Per-recipient receipt acknowledgement prevents repeats across Retry/restart; raised caps and new step executions have their own warning. No standing System Prompt rule is added.

The Workflow editor configures Workflow and step limits. Card detail shows limits and consumption per step (`card.get` `budget` keyed by step id); a waiting Card changes its snapshot through revision-checked `card.limits` (`card_limits`, and `step_limits` with `step`). Unrelated live Card revisions preserve unsaved limit inputs. Empty objects clear a scope. `_limits.py` owns normalization, consumption interpretation and thresholds; `_workflow_budgets.py` owns checks and notices, and `_workflow_sessions.py` coordinates reservation and delivery under the service lock.

## Attention and page

Entering `waiting` or receiving a Proposal sends an owner-keyed transient notification; both count toward the `board` page badge. Missing question/note text falls back to a readable Board-directed sentence. The Board is the durable attention record; generic notification routing belongs to the host/Accessor maps.

The default route `''` selects a native Project Factory. `/factory/<id>` contains its Board, with `/team`, `/discussions` and `/settings` destinations; `/task/<id>` and `/topic/<id>` show native details. `/cards` retains the standalone Workflow Board, `/card/<id>` its execution detail, and `/workflows[/<id>|/new]` the global editor. Native resources publish separate Factory/member/task/topic invalidations; native and Workflow controllers ignore each other's resource changes. Unknown native destinations and deleted records repair their route. Explicit Workflow deletion discards its local draft without requiring it to validate. List and history cursors are bounded; drafts survive unrelated refreshes, and stale revisions do not silently overwrite changed records.

Invalidation resource `workflows` reloads the list and the open Workflow, and a Workflow deleted elsewhere returns to `/workflows`. The page uses shared navigation, controls, autosave and its `factory.*` catalog (English only). A saved Workflow autosaves; a new one, from a blank start, a template or a copy, is created with an explicit button. The Workflow editor and Card progress share `WorkflowDiagram.svelte`: columns follow dependencies, dashed arrows show return targets, approval badges mark gated steps. Reordering/removing dependencies repairs invalid return targets, including downstream ones.

The Factory setup and new-member dialogs use the shared searchable Model picker with unpinned Model IDs from the Extension catalog. A new Leader requires an explicit Model; a new member can inherit.

Card detail shows each waiting step with its own Accept/Answer/Retry, Card-level actions, the step diagram with live statuses, Outputs (earlier documents fold), child and parent Cards, the trigger event, events, usage, per-step limits, participant History/Run streams (group `<card>.<step>`) and permission links.

## Agent-facing text

Before changing Factory text in `agent_text.py`, `_native_agent_text.py`, `_workflows.py`, the operation descriptions in `_values.py` / `_native_values.py` or `resources/extensions/factory/docs/usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. System rules, the step's task and continuation data remain separated and labeled; continuations become canonical notes, never fabricated user instructions.

## Verification routes

- Native persistence, completion graphs, receipts, review and additive migration: `tests/resources/extensions/factory/test_factory_native_store.py`; native authorization and exact Agent Tool dispatch: `test_factory_native_management.py`.
- Production Runtime native journeys (actual identity context, isolated grants, hand-in, consultation, task/Factory pause and restart): `test_factory_native_integration.py`. Interrupted record/callback recovery and task handoff: `test_factory_native_recovery.py`. Native counters/Usage and real admission boundaries: `test_factory_native_limits.py` and `test_factory_native_budget_admission.py`.
- Persisted Workflows, Cards, triggers' events and the setup migration: `tests/resources/extensions/factory/test_factory_store.py`.
- Public Tool origins, operator guards, Workflow management and scheduler admission: `test_factory_operations.py` in the same directory.
- Step graph execution, parallel steps, rework, approval gates, child Cards, stale participants, budgets and restart: `test_factory_execution.py`.
- Trigger sync, fires, webhook condensing and repository matching: `test_factory_triggers.py`; host trigger contracts: `tests/core/automation/test_cron_owned.py` and `tests/core/runtime/test_runtime_extension_host_triggers.py`.
- Rendered native Factory/team/task/discussion journeys and draft preservation: `webui/src/components/__tests__/FactoryPage.native.test.js`; retained Workflow editor, triggers, card decisions, limits and autosave: `FactoryPage.test.js`.
- Real Model evaluation uses the development instance and a disposable registered Project; verify repository state and canonical participant calls, not completion claims (`vbot-agent-eval` Skill).

## References

Read these only when your task matches - not by default.

- Changing Factory Agent-facing text -> `extensions/factory-agent-facing-text.md`
