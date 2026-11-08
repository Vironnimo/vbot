# Software Factory

The bundled `resources/extensions/factory/` Extension carries Project work from Cards through the steps of a per-Project Workflow to the user's acceptance; it loads through ordinary bundled discovery (API 27). Factory schedules Agents and records their results. It runs no Git commands, verifies no branches or commits, and has no special integration phase: the starter Workflows express integration as an ordinary step that waits for approval.

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
The Factory's Project-specific Workflows and default Workflow.

### Library template
A shared, versioned definition of one Step, a sequence of Steps or a complete Workflow, reusable in different Projects with local adjustments. Not an executing Card; each Project use remains linked to its adopted version (Library).

### Origin
The recorded source of a Card, deciding whether it starts automatically or waits as a Proposal.

## Ownership

- `_registration.py` declares the page, public and private Tools, settings, operations and required Session runtime callbacks; import and registration acquire no resources.
- `extension.py` owns the single serialized scheduler, host lifetime, change publication and attention. `_management.py` owns operator actions and read projections. `store.py` owns domain persistence, not canonical Session storage.
- `_workflows.py` owns Workflow validation, starter templates, dependency traversal and conversion from earlier setup formats. `_execution.py` owns participant configuration, step admission, submission guards, budgets and reconciliation.
- `_library.py` owns template extraction, linked insertion and update reconciliation within flat Workflows. `FactoryStore` owns its persistence; `_library_management.py` is an internal part of the existing management owner. The Library has no separate scheduler or execution service.
- `agent_text.py` owns Agent-facing definitions, instructions, notes and results; `_workflows.py` also holds the templates' task text. `ui/` owns page state and presentation through the generic Extension bridge; assets build to `web/` with the WebUI build.
- The existing Extension host owns user attention, current-input authority and owned approval visibility: these require current registration and canonical Run identity, not a Factory-local notification or approval store (`extensions/host.md`).

## Persistence and transitions

The host opens `ext.factory.factory`; tables are STRICT with explicit SELECT/INSERT columns and code-validated states. Setups (Workflows as JSON), Cards, Outputs, comments and audit events live here; participant history, receipts and usage remain canonical host reads. A Card change, its event and any attached comment/Output commit in one transaction. Existing SQL columns and earlier tables remain; the named `workflows` and `steps` migrations carry earlier setups forward. Output projections expose `step` and `step_name`, while stored columns retain their earlier names. Revision checks prevent stale operator drafts overwriting newer records; acceptance requires the displayed Card's `expected_revision`.

Origins are host-attributed, never a Tool argument. Operator operations queue; `factory_card` queues current human requests as `user` and currently authorized tasks as `automation`. Sub-Agents, owned participants and other initiative produce `agent` Proposals. API 24's `input_authority` reads the exact live Run; `host.review_context` includes old and ancestor requests and is unsuitable for this decision (`core/runtime/_shell_review_context.py`). Provenance stores the source Session address and Run id.

New execution uses `proposal -> queued -> working -> waiting -> done`, with discard available before completion. Steps run one at a time in list order when their dependencies are done; independent steps do not execute concurrently. `after` names earlier steps, and `returns_to` must be a direct or transitive predecessor. Results are passed through the dependency graph, not merely by list position. A step's `approval` gates its start. Waiting reasons `checkpoint`, `finished` and `no_agreement` are acceptable: Accept starts the gated step, finishes the Card, or overrules the step whose send-back limit was exhausted and continues.

Automatic changes requests rerun the target and its transitive dependents; unrelated finished steps stay done. `rounds` counts permitted automatic send-backs, not total reviews. Downstream approvals reset; the target keeps its own approval. Operator Send back can choose any step, resets send-back counts and records a comment; the default is the current completed step or latest completed step, following its return target when set. Answer resumes the blocked participant. Failures and exhausted execution budgets wait for the user. Discard cancels owned work without cleanup turns or repository changes, including when an earlier hand-in already scheduled cleanup waiting for that Run's terminal publication; deletion of a final Card archives its owned group.

The scheduler uses one in-process lock, selects lowest-position enabled-Project work, and enforces `wip_limit` (default 2). Waiting does not count. `max_waiting` defaults to 0 (unlimited). Operator resumes respect WIP: accepted/retried/sent-back Cards queue their intended action when slots are full. Acceptance that finishes a Card needs no slot and completes immediately. Queued resumes expose `queued_action` and cannot edit their objective while waiting. Live Extension configuration callbacks rerun admission without polling. Legacy `reviewing` and `integrating` states remain recognized for recovery, not new execution.

Admitted operator Card and setup mutations retain the service lock through persistence, publication and launch/drain scheduling even when their caller is cancelled. Cancellation while waiting for the lock starts no mutation.

Cards retain their Workflow with execution limits and Project-directory snapshot, participant Run ids and delivery state. Setup edits do not change started Cards; setup deletion refuses unfinished Cards. Before admission, the stored Workflow id must still exist: removal pauses the Card as `run_failed`, without substituting another Workflow. Restore that Workflow and Retry, or discard and recreate the Card with another selection. Creation resolves exact ids before display names. Reconcile upgrades Cards from older role/stage execution: work without a hand-in requeues; handed-in work waits for a decision, and an earlier branch hand-in becomes an Output for the approval-gated final integration step. Missing setup information pauses as `run_failed`. A Card whose start failed before choosing a Workflow requeues on Retry or Send back.

Preparing a continuation does not consume its pending intent: repeated request callbacks return the same delivery, and acknowledgment/startup reconcile its exact canonical Session receipt before clearing it. A newer canonical root Run failing before its request callback pauses the matching handoff for explicit retry, retaining guidance; sibling, older, foreign-generation and stale-epoch completions cannot claim it. Retry authorization is consumed before admission; an already failed canonical input gets a fresh continuation on the same History, while a new failure requires another user retry.

## Execution contracts

- One execution group per Card (`group_id == id`). Ordinary steps reuse their participant Session; steps with a return target create a fresh participant per execution (`<step>-r<n>`). Each step names an Identity Agent (`agent` -> `identity_profile`) or a Project Team member (`team` -> `repository_profile`). The Profile plus explicit `project_id` propagates Project Context; cwd alone does not. Prompt inspection shares execution's resolution.
- Setup reads/previews return effective Models keyed `<workflow>/<step>`, with an empty value when unresolved; saving refuses unresolved Models and names the step. Explicit Model/effort overrides apply after profile resolution. New setups default to enabled.
- Participants receive `factory_submit_work` and `factory_report_blocked`; only a step with `returns_to` receives `factory_request_changes`. Submission records a required summary and optional `result` (up to 100000 characters); change requests record concrete findings. Factory does not validate repository facts in these results. Agent task text and Project instructions govern read-only work, isolation, checks, commits and integration; normal Permission Rules remain in force.
- `run_finished` fires before its Run releases the Session. Next-step admission runs in tracked work after release. Successful private Tools use `context.request_turn_end()`; no extra Model-facing channel is introduced.
- Temporary groups refuse participant creation while open. Fresh admission drains/closes/reopens through the existing owner API; obsolete participants and late callbacks cannot move a newer step.
- Pending permission requests use owner-scoped host reads and link to the existing Session approval UI; Factory neither auto-approves nor widens rules. The page refreshes on approval invalidation, not polling.
- Startup reconciles Cards and groups with bounded recovery and archives orphan groups. Quiesce drains work before registration retirement. Closed done/discarded groups retain readable Sessions until Card deletion.

## Execution limits

User decision 2026-11-08: optional independent limits for Runs, admitted Tool calls, input plus output Tokens, and Model cost in USD. Workflow `limits` apply to the entire Card; each Step's `limits` apply to that Step execution. Omitted or zero values are unlimited, including all new defaults. A new Step or rework resets its Step counters and override; Card totals persist. Pause, Answer, Retry and restart preserve consumption. The first exhausted cap pauses the Card as `budget_exhausted`; `runtime.limit_hit` identifies scope, metric, used, limit and reason.

The service lock serializes Run admission and durable Tool reservations. The required owner Tool callback covers nested programmatic calls and owned Sub-Agents before any effect; refused calls do not consume capacity. Root admissions and observed child Runs are deduplicated, including validated automatic root Runs that fail before their first request callback. Tokens/cost include auxiliary requests and estimates from settled canonical Usage at stored prices; a running request can overshoot, and unavailable required usage pauses rather than counting as free. Required admission also runs before each physical Chat request, after automatic Compaction and retry waiting, so settled consumption blocks later requests within the same logical Model step. Card totals also include group-level Model work; Step totals select that execution's Run ids. Migration preserves saved turn caps as Step Run limits, reconstructs recorded Run admissions and imports retained Tool consumption once. Step membership comes from actual new-Step transitions, not the old turns counter that Retry and Answer reset. A corrective migration repairs already upgraded Cards and adjusts only their affected Step Tool counts.

At the next root participant Model request, a canonical Note warns around 80% consumption (at least one unit before integer limits). It names each affected scope, usage, cap and remaining capacity, then asks for completion or a blocked hand-in with progress, remaining work and needed budget. Run warnings explain that the current Run is counted and can finish. A single request or parallel Tool batch can skip the warning threshold. A due warning accompanies a pending continuation in the same receipt-backed delivery, including the last allowed Run. Per-recipient receipt acknowledgement prevents repeats across Retry/restart; raised caps and new Step executions have their own warning. No standing System Prompt rule is added.

Setup and Library editors configure Workflow/Step limits. Card detail shows effective limits and consumption; waiting Cards can change their snapshot through revision-checked `card.limits`, independently by scope. Empty objects clear that scope's caps. Edits flush before Retry and do not reset counters or alter the Project setup. `_limits.py` owns normalization, consumption interpretation and threshold calculation; `_execution.py` owns reservation and delivery under its existing lock.

## Library

User decision 2026-11-07: shared templates cover individual Steps, Step sequences and whole Workflows. Projects keep a connection to the adopted version and their local adjustments; new versions are adopted explicitly per Project, with conflicts visible. Already started Cards keep their Workflow snapshot.

The existing Factory database owns template identities and immutable published versions. Publishing and archiving use optimistic revisions; archive hides a template from ordinary discovery while historical versions and existing Project uses remain readable. Built-in starter Workflows stay available independently. The additive `library_links` migration declares older code incompatible because its Workflow normalizer would discard source metadata on a setup save.

Insertion produces ordinary flat Steps with remapped ids and a source record holding the template version, stable step mapping and baseline. Multiple uses of one template have independent mappings. Dependency wiring connects an inserted sequence to its surrounding Steps; execution and send-back use the existing Workflow contracts. Publishing an extracted Workflow or selection removes Project source links and external dependencies, so templates do not retain references to another Project's Steps.

Linking a newly published template over existing uses requires explicit `replace_sources`; it replaces covered sources only when the selection contains every surviving member of each overlapping use. The page explains the replacement and refuses partial overlap before publication. This permits publishing a composed Workflow without overlapping or nested source ownership.

Update preview compares the adopted baseline, current Project values and an explicitly chosen published version. Local-only changes survive; overlapping field or structural changes require choices. Applying an update pins the previewed version and refuses unresolved conflicts or an invalid resulting graph. Composition operations return drafts; `setup.update` remains the only Project save, with its normal Model validation and optimistic revision. Library publication alone never changes a Project or Card.

## Attention and page

Entering `waiting` or receiving a Proposal sends an owner-keyed transient notification; both count toward the `board` page badge. Missing question/note text falls back to a readable Board-directed sentence. Closing a group invalidates its Card after the Run ends, so status and usage do not remain live. The Board is the durable attention record; generic notification routing and retirement belong to the host/Accessor maps.

Routes are `''`, `/card/<id>`, `/setups[/<project_id>]` and `/library[/<template_id>|/new]`. The page uses shared navigation, controls, autosave and its `factory.*` catalog; invalidations preserve active drafts. Setup editing and Card progress share `WorkflowDiagram.svelte`: solid arrows show dependencies, dashed arrows show return targets and their limits, and approval badges mark gated steps. Clicking a setup node opens its task, Agent/Model/effort and dependency editor. Reordering/removing dependencies repairs invalid return targets, including downstream ones.

Library editing reuses the Step editor and diagram. Template drafts survive navigation within the page; publication is explicit, with immutable history and archive/restore actions. Project setup can publish a whole Workflow or consecutive selected Steps, link that originating selection without inserting copies, or insert a template at a chosen position. Republishing an existing use retains its source identity even after local membership changes; repeated uses require choosing the source. Linked uses show their adopted version and available updates, with readable field comparisons and explicit conflict choices before autosaving the resulting Project Workflow.

Card detail shows progress from `done_steps` and the current step, summaries/documents/findings from Outputs, event history, usage, participant History/Run streams and permission links. Earlier documents fold while the latest document per step stays open. Send back offers every step. There are no special branch, commit-range or integration displays or setup fields.

## Agent-facing text

Before changing Factory text in `agent_text.py`, `_workflows.py` or the Factory section of `vbot-docs/references/extension-usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. System rules, the step's task and continuation data remain separated and labeled; continuations become canonical notes, never fabricated user instructions.

## Verification routes

- Persisted Card/setup/audit and Library version contracts: `tests/resources/extensions/factory/test_factory_store.py`.
- Library extraction, insertion, link validation and three-way adoption: `tests/resources/extensions/factory/test_factory_library.py`.
- Public Tool origins, operator guards and scheduler admission: `tests/resources/extensions/factory/test_factory_operations.py`.
- Step execution, rework, approval gates, stale participants, budgets and restart: `tests/resources/extensions/factory/test_factory_execution.py`.
- Rendered Board, diagram, actions, results and autosave: `webui/src/components/__tests__/FactoryPage.test.js`; Library publication, reuse and adoption: `FactoryPage.library.test.js` in the same directory. Shared notification/bridge and host tests live with their owners.
- Real Model evaluation uses the development instance and a disposable registered Project; verify repository state and canonical participant calls, not completion claims (`vbot-agent-eval` Skill).

## References

Read these only when your task matches - not by default.

- Changing Factory Agent-facing text -> `extensions/factory-agent-facing-text.md`
