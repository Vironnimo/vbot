# Cron Jobs

Schedule recurring or one-time agent prompts.

```bash
vbot cron list
vbot cron create <agent> --prompt <text> (--cron "<expression>" | --at <iso-datetime>) [--session <session-id>]
vbot cron update <job-id> [--agent <agent>] [--prompt <text>] [--cron "<expression>" | --at <iso-datetime>] [--session <session-id>] [--status active|paused]
vbot cron delete|enable|disable <job-id>
```

- `create` requires exactly one of `--cron` (exactly five fields: minute, hour, day of month, month, weekday; minimum cadence one minute) or `--at` (one-time ISO 8601 datetime); the schedule type is derived from the flag.
- `<agent>` (and `update --agent`) takes a bare identity agent or `agent@projekt`; a project-targeted job runs in that project.
- `cron list` includes active, paused, failed, completed, and missed history, and shows id, target (same address form), status, schedule, next fire time, last outcome, and a prompt preview — read job ids from there.
- `create`, `update`, `enable`, and `disable` return the saved Cron job with its id, target, schedule, status, and projected next fire time; use that output as the immediate verification result.
- `--session` pins the job to an existing Session owned by the target. Without it, every fire creates a fresh Session.
- Cron expressions and offset-free timestamps passed to `--at` use the server's current IANA system timezone. A missed one-time job does not catch up after a restart and is recorded as `missed`.
- A recurring job waits for its Run to finish before scheduling its next occurrence, so fires never overlap for the same job. Repeated Run failures are recorded and eventually stop the job as `failed`.
- A cron job targeting a project agent blocks `project rm` for that project (`project_in_use`) — retarget or delete the job first.

```bash
vbot cron create assistant --prompt "Check the news" --cron "0 9 * * *"
vbot cron create builder@vbot --prompt "Nightly build" --cron "0 2 * * *"
vbot cron create assistant --prompt "Remind me about the deadline" --at 2026-07-01T09:00:00
vbot cron update <job-id> --status paused
```
