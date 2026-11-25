# Software Factory

The bundled `resources/extensions/factory/` Extension owns per-Project Software Factories with a permanent Leader, Leader Chat, Specialist Agents, recurring or on-demand Jobs, and canonical Session Job Runs; it loads through ordinary bundled discovery and uses canonical host Sessions and Runs.

User decisions 2026-11-25: each registered Project can have one Factory with a permanent Leader and optional Specialist Agents. Work is modeled as minimal Jobs (required `name` and `prompt`; optional `description`, `agent_member_id` with Leader fallback, `enabled`, and `schedule`/`webhook` triggers) rather than Workflow graphs, Cards, or Kanban tasks. Job Runs execute in canonical Sessions, complete normally as `done` or report status via `factory_job_status`, surface "near the loop" attention, and resume via direct Session replies.

## Terms

Shared Agent, Session, Run, Tool and Project vocabulary lives in GLOSSARY.

### Factory
A permanent working environment for a registered Project, with a Leader, Specialist members, Leader Chat, and Jobs. Not the repository-scanned Project Team defined in GLOSSARY.

### Factory Team
The persistent Identity Agent memberships of one Factory (its Leader and Specialists). Not the scanned Project Team defined in GLOSSARY, and not temporary Sub-Agents.

### Leader
The permanent primary member of a Factory who chats with the user in the Project overview and manages Jobs via `factory_jobs`.

### Specialist
A permanent Factory member (linked or Factory-created Identity Agent) with a role, standing instructions, and optional Model or access boundaries.

### Job
A reusable named prompt with optional description, assigned member (defaults to the Leader), enabled flag, and schedule or webhook triggers.

### Job Run
One canonical Session execution of a Job (`queued`, `running`, `needs_attention`, `done`, `failed`, `cancelled`) with an origin (`manual`, `leader`, `schedule`, `webhook`, `public_tool`).

### Trigger
A Job's `schedule` or `webhook` that queues Job Runs automatically; Cron owns schedule and webhook mechanics (`automation/cron.md`, `extensions/host.md`).

## Ownership

- `_registration.py` declares page `board` ("Software Factory"), setting `wip_limit` (default `2`), public `factory` Tool, private Session Tools (`factory_job_status`, `factory_jobs`, `factory_settings`, `factory_members`), Session runtime callbacks, and management operations (with `job.secret` as a secret operation); import and registration acquire no resources.
- `extension.py` owns the single serialized `asyncio.Lock` scheduler (`NativeFactoryExecution.schedule` and `FactoryJobExecution.schedule`), Project resolution, host lifetime, `_runtime_owner` dispatch (`fjob.*` -> `job_execution`, `native.*` -> `native.sessions`), and the `board` attention badge.
- `_management.py` dispatches `catalog`, `job.*`/`job_run.*`/`dashboard.get` (`_job_management.py`), and `agent.*`/`factory.*`/`member.*`/`conversation.*` (`_native_management.py`).
- `store.py`, `_jobs_schema.py`, `_jobs_store.py`, and `_native_store.py`: `host.open_database("factory", ...)` opens `ext.factory.factory`. `_jobs_schema.py` adds Generation 1 tables (`factory_jobs`, `factory_job_runs`, `factory_job_trigger_receipts`) alongside `native_*` tables (`_native_schema.py`). Legacy Workflow, Card, setup, and Library tables remain on disk and inert; declared `MIGRATIONS` stay intact so existing databases open cleanly.
- `_job_triggers.py` reconciles Job triggers to host triggers keyed `job:<factory_id>:<job_id>:<trigger_id>`, condenses webhook payloads (GitHub-aware, up to 20 commits, 4000-char field limit, up to 20 runs per fire), and deduplicates fires via `factory_job_trigger_receipts`.
- `_job_execution.py` admits queued Job Runs within `wip_limit`, creates/reuses canonical Session group `fjob.*` with `identity_agent` and `identity_incarnation`, settles normal completion to `done`, handles `reply_run` and `cancel_run`, reconciles interrupted `running` runs on startup, and publishes `job_runs` changes and attention notifications.
- `_job_tools.py` handles private Session Tools `factory_job_status` (`fjob.*` runs) and `factory_jobs` (Leader-authenticated via `native.*` execution). `_public_tool.py` handles the public cross-Factory `factory` CRUD Tool.
- `_native_management.py`, `_native_identities.py`, `_native_execution.py`, `_native_sessions.py`, `_native_discussions.py`, and `_conversations.py` own Factory and Specialist CRUD, durable identity reservations/incarnation proofs, and Leader Chat (`native.*` group, topic-backed Session).
- `_job_agent_text.py` and `_native_agent_text.py` own Agent-facing Tool definitions, run instructions, Leader job catalog context, and bounded data wrappers.
- `ui/` (`FactoryPage.svelte`, `pageModel.svelte.js`, `OverviewTab.svelte`, `JobsTab.svelte`, `TeamTab.svelte`, `SettingsTab.svelte`, `JobRunDetail.svelte`, `TriggerEditor.svelte`, `RecordEditor.svelte`, `FactoryChat.svelte`) owns page state and presentation through the generic Extension bridge; assets build to `web/`.

## Factories, Team, and Leader Chat

- Each registered Project has at most one Factory in `native_factories` (`project_id` UNIQUE), with a `leader_member_id`, `remit`, `capacity`, `state` (`active` or `paused` or `archived`), and `config` boundaries (`allowed_models`, `allowed_skills`, `allowed_tools`, `denied_tools`, `permission_ceiling_profile`, `permission_ceiling`).
- Factory creation (`factory.create`) reuses an existing personal Agent (`leader_agent_id`) or creates a new Leader (`leader_name`, `leader_model`). Specialist creation (`member.create`) allocates a durable Factory-owned Identity Agent via `native_identity_reservations`; `member.add` links an existing personal Agent without altering its global profile.
- A new Leader or Specialist sets no Tool access or Skills of its own: the Factory passes only `name` and `model` to `host.identity_agents.create` (`_native_identities.py`), so the Identity Agent inherits the shared defaults (`defaults.agent`, `settings.md`) whenever it is read and follows later changes to them. A member's own `allowed_tools`, `allowed_skills` and `denied_tools` stay optional Factory-local narrowing, intersected at Session build with the Identity Agent's selection and the Factory boundary; a linked personal Agent keeps its own configuration (`test_factory_public_tool.py` fake `create_identity`).
- Native and Job Sessions use `identity_agent` and `identity_incarnation` (`agent_proof`) owned-execution mode so the host attaches the Project's Working Project while preserving the Agent's Workspace, personal Memory, private Skills, and SOUL.
- Leader Chat uses `conversation.get`, `conversation.send`, and `conversation.stop` backed by `native_topics`/`native_posts` and a `native.*` Session group. The Leader receives `factory_jobs`, `factory_members`, `factory_settings`, and the current Factory's job catalog in its runtime instructions.
- `factory_settings` and `factory_members` are private Session Tools scoped to the active Factory execution; Factory lifecycle, remit, capacity, and ceilings remain operator-only.

## Jobs, Triggers, and Job Runs

- Jobs (`factory_jobs`) require `name` (1-200 chars) and `prompt`; `description`, `agent_member_id` (empty string means Leader fallback), `enabled` (default `true`), and `triggers` (`schedule` or `webhook`) are optional.
- Manual/CLI (`job.trigger`), Leader (`factory_jobs action='trigger'`), and cross-Factory (`factory action='trigger_job'`) triggers accept an optional `member_id` override (resolved by member id, role, or name) and optional `input_note`.
- `_job_triggers.sync` reconciles enabled/disabled triggers with host Cron (`extensions/host.md`). `job.secret` reads or rotates a webhook trigger's signing secret through the host; secrets never enter Factory SQLite records.
- Job Runs (`factory_job_runs`) start `queued` and are admitted to `running` under the single Extension-wide `wip_limit` setting (default `0` = unlimited).
- Inside a `fjob.*` Session, the worker receives `factory_job_status` (and never `factory_jobs`, `factory_settings`, or `factory_members`):
  - Calling `factory_job_status` with `needs_attention` (requires `question`), `failed` (requires `reason`), or `done` (optional `summary`) transitions the run, ends the turn (`context.request_turn_end()`), and publishes the change.
  - Ending normally without calling `factory_job_status` settles the run to `done`, extracting the final assistant answer (up to 4000 chars) as `summary`. Cancelled or non-success Run outcomes settle to `cancelled` or `failed`.
- `before_model_request` refuses calls on runs already in a final state (`done`, `failed`, `cancelled`).
- `job_run.reply` sends an operator message directly into the run's canonical Session (`TemporaryRunInput("continuation", ...)`), transitioning `needs_attention` or `failed` runs back to `running` (`done` and `cancelled` runs cannot be resumed). `job_run.cancel` closes active `fjob.*` groups outside the service lock and marks the run `cancelled`. `job_run.dismiss` acknowledges a `failed` run to clear it from Needs Attention and the page badge.
- The Leader's `factory_jobs` Tool supports `list`, `get`, `create`, `update`, `delete`, `trigger`, `runs` (with optional `state` filter), `cancel`, and `reply`.
- On startup, `reconcile` marks any persisted `running` Job Run whose host Run is no longer live as `failed` ("Interrupted by server restart.").

## Public `factory` Tool

- `_public_tool.py` exposes the `factory` Tool to ordinary Sessions for cross-Factory CRUD across all registered Projects:
  - Factories: `list_factories`, `get_factory`, `create_factory`, `update_factory`, `archive_factory`
  - Specialists (Members): `list_members`, `create_member`, `update_member`, `retire_member`
  - Jobs & Runs: `list_jobs`, `get_job`, `create_job`, `update_job`, `delete_job`, `trigger_job`, `list_runs`
- Each trigger in `create_job` or `update_job` takes a `type` (`schedule` or `webhook`) and optional `id`, `title`, `schedule`, and `enabled` fields. The Factory generates omitted trigger IDs.
- Read, member, job, update, and archive actions resolve the target Factory from either `factory_id` or `project_id`. `trigger_job` queues a run with `origin: "public_tool"`.

## Attention and WebUI

- The `board` page badge sums `needs_attention` and `failed` Job Runs across active Factories plus pending host approvals on active `fjob.*` runs. Entering `needs_attention` or `failed` sends a host notification routed to `/job-run/<id>`.
- `FactoryPage.svelte` routes: `/project/<id>` (uninitialized Project setup), `/factory/<id>` (Overview: full-height Leader Chat with `ContextUsageRing`, direct `openSession`, and `New Session` beside independent Dashboard panels for attention, running, upcoming, and final-state recent runs), `/factory/<id>/jobs` (Job CRUD, trigger editor, manual run), `/factory/<id>/team` (Leader + Specialist CRUD, `Edit Agent` profile link, and inline role & instructions editor without per-member raw tool/skill text fields), `/factory/<id>/settings` (name, remit, capacity, permission ceiling, pause/archive), and `/job-run/<id>` (`JobRunDetail.svelte`: status, summary/question/reason, canonical Session embed, direct reply resume, cancel).

## Agent-facing text

Before changing Factory Agent-facing text in `resources/extensions/factory/_job_agent_text.py`, `_native_agent_text.py`, `_public_tool.py`, `_job_management.py`, `_native_values.py`, or `resources/extensions/factory/docs/usage.md`, read `extensions/factory-agent-facing-text.md` for sentence reasons. Update its row for every sentence you change and add one for every sentence you add.

## Verification routes

- Jobs, Runs, and trigger receipts persistence: `tests/resources/extensions/factory/test_factory_jobs_store.py`.
- Trigger sync, webhook payload condensing, and fire deduplication: `tests/resources/extensions/factory/test_factory_job_triggers.py`.
- Job Run scheduling, WIP limit, normal completion, `reply_run`, `cancel_run`, startup reconciliation, and attention count: `tests/resources/extensions/factory/test_factory_job_execution.py`.
- Private Session Tools (`factory_job_status`, Leader `factory_jobs`): `tests/resources/extensions/factory/test_factory_job_tools.py`.
- Management operations (`job.*`, `job_run.*`, `dashboard.get`): `tests/resources/extensions/factory/test_factory_job_management.py`.
- Public cross-Factory `factory` CRUD Tool: `tests/resources/extensions/factory/test_factory_public_tool.py`.
- Extension registration, settings, and runtime routing: `tests/resources/extensions/factory/test_factory_extension_wiring.py`.
- WebUI Dashboard, Leader Chat, Jobs, Team, Settings, and Job Run detail: `webui/src/components/__tests__/FactoryPage.test.js`.

## References

Read these only when your task matches - not by default.

- Changing Factory Agent-facing text -> `extensions/factory-agent-facing-text.md`
