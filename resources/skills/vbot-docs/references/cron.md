# Cron jobs

Schedule an Agent Run at a time, repeatedly, or at every occurrence of a calendar event. A bare Agent id targets an Identity Agent; `agent@project` targets that Project Agent.

```bash
vbot cron list
vbot cron show <job-id>
vbot cron create <agent> --prompt <text> [--name <name>] (--cron "<five fields>" | --every <minutes> | --at <iso-datetime> | --event <event-id> [--event-time "<event time>"]) [--repeat <count>] [--session <session-id>]
vbot cron update <job-id> [--agent <agent>] [--name <name>] [--prompt <text>] [--cron "<five fields>" | --every <minutes> | --at <iso-datetime> | --event <event-id>] [--event-time "<event time>"] [--repeat <count>] [--session <session-id> | --clear-session] [--status active|paused]
vbot cron enable <job-id>
vbot cron disable <job-id>
vbot cron delete <job-id>
```

Create requires exactly one schedule: `--cron` is minute/hour/day/month/weekday, `--every` is a positive whole-minute interval, `--at` is a one-time ISO timestamp, and `--event` is a calendar event id. Cron and offset-free timestamps use `vbot config get server.timezone`. Choose a future date for one-time jobs.

An `--event` job runs once at every occurrence of the event. `--event-time` says when: `start` or `end`, optionally `+` or `-` a duration in minutes, hours or days of at most 31 days, such as `"start - 30m"` or `"end + 1h"`; the default is `start`. The job follows moved occurrences, skips removed ones and is deleted together with its event. The Run receives the prompt together with the event's title, time, location and description. `--repeat` is not available for it. On update, `--event-time` alone changes the time of a job that already has an event.

Names need not be unique and default from the prompt when omitted. `list` returns exact ids, schedule, remaining fires, next fire, Session target, last outcome/error and a shortened prompt. Use `show` for the complete prompt and saved record before editing. Create/update/enable/disable report the saved state; that does not prove the scheduled Run has executed.

Recurring schedules are unlimited unless `--repeat` sets a finite number of future fires. On update it replaces the remaining count. A recurring job waits for its Run to finish before scheduling its next occurrence; repeated failures eventually stop it. Inspect `last_outcome` and `last_error` when troubleshooting.

A fire missed while vBot was not running, for example while the server was off or the computer was asleep, starts once when vBot runs again, for the most recent missed time. Earlier missed times do not run separately. An `--event` job instead starts late for each missed occurrence while that occurrence is still relevant: until the event starts when the job was due before it, until the event ends when due during it, and otherwise until the next occurrence starts. That Run receives a System Reminder with the due time, the actual start time and the earlier missed times. To have a late Run skip the work, say so in the prompt, for example "Skip this if it starts more than 30 minutes late."

On create, omitting `--session` creates a fresh Session per fire. On update, omission preserves the current target; `--clear-session` restores fresh Sessions. A pinned Session must belong to the chosen Agent. Other omitted update fields stay unchanged.

Write self-contained prompts stating the work, relevant constraints and expected report. A Project-targeted job can block Project removal; retarget or delete it first.

```bash
vbot cron create assistant --name "Morning news" --prompt "Check the news and report relevant changes" --cron "0 9 * * *"
vbot cron create assistant --prompt "Check the build and report a changed result" --every 15 --repeat 4
vbot cron update <job-id> --status paused
```
