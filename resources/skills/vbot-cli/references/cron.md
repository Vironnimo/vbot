# Cron Jobs

Schedule recurring or one-time agent prompts.

```bash
vbot cron list
vbot cron create <agent> --prompt <text> (--cron "<expression>" | --at <iso-datetime>) [--timezone <iana-timezone>] [--session <session-id>]
vbot cron update <job-id> [--agent <agent>] [--prompt <text>] [--cron "<expression>" | --at <iso-datetime>] [--timezone <iana-timezone>] [--session <session-id>] [--status active|paused|completed]
vbot cron delete|enable|disable <job-id>
```

- `create` requires exactly one of `--cron` (recurring cron expression) or `--at` (one-time ISO 8601 datetime); the schedule type is derived from the flag.
- `<agent>` (and `update --agent`) takes a bare identity agent or `agent@projekt`; a project-targeted job runs in that project.
- `cron list` shows id, target (same address form), status, schedule, next fire time, and a prompt preview — read job ids from there.
- `--session` pins the job to a fixed session instead of a job-managed one.
- A cron job targeting a project agent blocks `project rm` for that project (`project_in_use`) — retarget or delete the job first.

```bash
vbot cron create assistant --prompt "Check the news" --cron "0 9 * * *" --timezone Europe/Berlin
vbot cron create builder@vbot --prompt "Nightly build" --cron "0 2 * * *"
vbot cron create assistant --prompt "Remind me about the deadline" --at 2026-07-01T09:00:00
vbot cron update <job-id> --status paused
```
