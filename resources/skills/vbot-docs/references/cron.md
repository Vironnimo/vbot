# Cron jobs

Schedule an Agent Run at a time or repeatedly. A bare Agent id targets an Identity Agent; `agent@project` targets that Project Agent.

```bash
vbot cron list
vbot cron show <job-id>
vbot cron create <agent> --prompt <text> [--name <name>] (--cron "<five fields>" | --every <minutes> | --at <iso-datetime>) [--repeat <count>] [--max-delay <minutes>] [--session <session-id>]
vbot cron update <job-id> [--agent <agent>] [--name <name>] [--prompt <text>] [--cron "<five fields>" | --every <minutes> | --at <iso-datetime>] [--repeat <count>] [--max-delay <minutes> | --no-max-delay] [--session <session-id> | --clear-session] [--status active|paused]
vbot cron enable <job-id>
vbot cron disable <job-id>
vbot cron delete <job-id>
```

Create requires exactly one schedule: `--cron` is minute/hour/day/month/weekday, `--every` is a positive whole-minute interval, and `--at` is a one-time ISO timestamp. Cron and offset-free timestamps use `vbot config get server.timezone`. Choose a future date for one-time jobs.

Names need not be unique and default from the prompt when omitted. `list` returns exact ids, schedule, remaining fires, late start limit, next fire, Session target, last outcome/error and a shortened prompt. Use `show` for the complete prompt and saved record before editing. Create/update/enable/disable report the saved state; that does not prove the scheduled Run has executed.

Recurring schedules are unlimited unless `--repeat` sets a finite number of future fires. On update it replaces the remaining count. A recurring job waits for its Run to finish before scheduling its next occurrence; repeated failures eventually stop it. Inspect `last_outcome` and `last_error` when troubleshooting.

A fire missed while vBot was not running, for example while the server was off or the computer was asleep, starts once when vBot runs again, for the most recent missed time. Earlier missed times do not run separately. That Run receives a System Reminder with the due time, the actual start time and the earlier missed times. `--max-delay <minutes>` skips a missed fire that would start more than that many minutes late; `--max-delay 0` skips every missed fire, and `--no-max-delay` removes the limit. Set a limit only when the user asks for it or a late Run would be useless. A skipped fire sets `last_outcome` to `missed` and states the reason in `last_error`.

On create, omitting `--session` creates a fresh Session per fire. On update, omission preserves the current target; `--clear-session` restores fresh Sessions. A pinned Session must belong to the chosen Agent. Other omitted update fields stay unchanged.

Write self-contained prompts stating the work, relevant constraints and expected report. A Project-targeted job can block Project removal; retarget or delete it first.

```bash
vbot cron create assistant --name "Morning news" --prompt "Check the news and report relevant changes" --cron "0 9 * * *"
vbot cron create assistant --prompt "Check the build and report a changed result" --every 15 --repeat 4
vbot cron update <job-id> --status paused
```
