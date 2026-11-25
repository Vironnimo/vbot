# Software Factory

Software Factory gives each registered Project a permanent team—one **Leader** Agent and optional **Specialist** Agents—plus recurring or on-demand **Jobs** and a **Dashboard** (`Software Factory` page).

## Team: Leader and Specialists

- Each Project has at most one Factory, created with an existing personal Agent (`leader_agent_id`) or a new Leader (`leader_name`, `leader_model`).
- **Leader:** Chats with the user in the Project overview, orchestrates work, and manages the Factory's Jobs through its private `factory_jobs` Tool.
- **Specialists:** Factory members with a `name`, `role`, `instructions`, and optional `model`. A member created by the Factory belongs to that Project's team; open **Team -> Edit Agent** in the UI to change its Agent profile settings (name, Model, thinking effort).
- Retiring a member keeps historical records; archiving a Factory keeps its records while stopping new work.

## Jobs, Triggers, and Runs

- **Jobs** define reusable work: `name`, `prompt`, optional `description`, assigned `agent_member_id` (defaults to the Leader when omitted), `enabled`, and `triggers`.
- **Triggers** on a Job can be:
  - `schedule`: cron expressions (e.g. `0 9 * * 1-5`), intervals (`every 2h`), relative delays (`in 30m`), or ISO timestamps.
  - `webhook`: generates a webhook URL and signing secret; incoming deliveries create Job Runs with condensed event details.
- **Job Runs** move through `queued` -> `running` -> `done` / `needs_attention` / `failed` / `cancelled`:
  - Workers call `factory_job_status` to report `needs_attention` (with a `question` for the user), `failed` (with a `reason`), or `done` (with a dashboard `summary`). Finishing normally without calling `factory_job_status` marks the run `done`.
  - Runs in `needs_attention` notify the user and wait on the Dashboard until answered or cancelled.

## Public `factory` Tool

Any Agent with the `factory` Tool can manage Factories, Specialists, and Jobs across all Projects (for example, creating a Factory for a new Project or copying Jobs and Specialists from one Project to another):

- **Factories:** `list_factories`, `get_factory`, `create_factory`, `update_factory`, `archive_factory`
- **Members (Specialists):** `list_members`, `create_member`, `update_member`, `retire_member`
- **Jobs & Runs:** `list_jobs`, `get_job`, `create_job`, `update_job`, `delete_job`, `trigger_job`, `list_runs`

Read, member, job, update, and archive actions accept either `factory_id` or `project_id` to identify the target Factory. `trigger_job` queues a run with `origin: "public_tool"` and accepts an optional `member_id` override (id, role, or name) and `input_note`.

## CLI and Operations

Use `vbot extensions operations factory` to inspect the full operation catalog and `vbot extensions run factory <operation> --help` for flat CLI arguments.

```bash
vbot extensions run factory factory.list
vbot extensions run factory factory.create --project-id <project-id> --leader-agent-id <agent-id>
vbot extensions run factory member.create --factory-id <factory-id> --name Reviewer --role reviewer --request-id <id>
```
