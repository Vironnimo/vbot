# Cron jobs

Schedule an Agent Run at a time, repeatedly, at every occurrence of a calendar event, or whenever a request reaches the job's webhook URL. A bare Agent id targets an Identity Agent; `agent@project` targets that Project Agent.

```bash
vbot cron list
vbot cron show <job-id>
vbot cron create <agent> --prompt <text> [--name <name>] (--cron "<five fields>" | --every <minutes> | --at <iso-datetime> | --event <event-id> [--event-time "<event time>"] | --webhook) [--repeat <count>] [--session <session-id>] [--deliver app|chat|none [--channel <channel-id> --chat <chat-id> [--thread <thread-id>]]] [--watch <command-or-url>]
vbot cron update <job-id> [--agent <agent>] [--name <name>] [--prompt <text>] [--cron "<five fields>" | --every <minutes> | --at <iso-datetime> | --event <event-id> | --webhook] [--event-time "<event time>"] [--repeat <count>] [--session <session-id> | --clear-session] [--status active|paused] [--deliver app|chat|none [--channel <channel-id> --chat <chat-id> [--thread <thread-id>]]] [--watch <command-or-url> | --clear-watch]
vbot cron enable <job-id>
vbot cron disable <job-id>
vbot cron delete <job-id>
vbot cron secret <job-id> [--rotate]
```

Create requires exactly one schedule: `--cron` is minute/hour/day/month/weekday, `--every` is a positive whole-minute interval, `--at` is a one-time ISO timestamp, and `--event` is a calendar event id. Cron and offset-free timestamps use `vbot config get server.timezone`. Choose a future date for one-time jobs.

An `--event` job runs once at every occurrence of the event. `--event-time` says when: `start` or `end`, optionally `+` or `-` a duration in minutes, hours or days of at most 31 days, such as `"start - 30m"` or `"end + 1h"`; the default is `start`. The job follows moved occurrences, skips removed ones and is deleted together with its event. The Run receives the prompt together with the event's title, time, location and description. `--repeat` is not available for it. On update, `--event-time` alone changes the time of a job that already has an event.

A `--webhook` job runs whenever a POST request reaches its URL, `/api/hooks/<job-id>` on vBot's server address, and proves the job's secret: with the header `Authorization: Bearer <secret>`, or as a GitHub webhook whose secret is the job's secret (`X-Hub-Signature-256`). vBot makes the secret; create prints the URL, who can reach it and the secret, `vbot cron secret <job-id>` shows them again, and `--rotate` replaces the secret, after which requests with the old one are refused. Services on the internet, such as GitHub, reach the URL only while vBot is open to the internet with a public URL (`vbot application access show`). The Run receives each request's arrival time, selected headers (such as `X-GitHub-Event`) and body, at most 16,000 characters of it, as data; a body over 256 KB is refused. Requests that arrive while a Run is busy wait and reach the next Run together, at most 10 requests and 16,000 characters; the oldest are dropped first and the Run is told. `--repeat` counts Runs. Paused, deleted and non-webhook jobs answer like unknown URLs.

Names need not be unique and default from the prompt when omitted. `list` returns exact ids, schedule, remaining fires, next fire, Session target, delivery, watch, last outcome/error and a shortened prompt. Use `show` for the complete prompt and saved record before editing. Create/update/enable/disable report the saved state; that does not prove the scheduled Run has executed.

Recurring schedules are unlimited unless `--repeat` sets a finite number of future fires. On update it replaces the remaining count. A recurring job waits for its Run to finish before scheduling its next occurrence; repeated failures eventually stop it. Inspect `last_outcome` and `last_error` when troubleshooting.

A fire missed while vBot was not running, for example while the server was off or the computer was asleep, starts once when vBot runs again, for the most recent missed time. Earlier missed times do not run separately. An `--event` job instead starts late for each missed occurrence while that occurrence is still relevant: until the event starts when the job was due before it, until the event ends when due during it, and otherwise until the next occurrence starts. That Run receives a System Reminder with the due time, the actual start time and the earlier missed times. To have a late Run skip the work, say so in the prompt, for example "Skip this if it starts more than 30 minutes late."

On create, omitting `--session` creates a fresh Session per fire. On update, omission preserves the current target; `--clear-session` restores fresh Sessions. A pinned Session must belong to the chosen Agent. Other omitted update fields stay unchanged.

`--deliver` says where each Run's final answer goes: `app` shows the Run's Session as unread in vBot and notifies the user, `chat` sends it to a Channel chat named by `--channel` (an id from `vbot channel list`, a Channel of the job's Agent) and `--chat` (such as one of the Channel's `allowed_chat_ids`), optionally into `--thread`, and `none`, the default on create, leaves it in the Run's Session. A Run that answers exactly `[SILENT]` delivers nothing, so a prompt can say to stay silent when there is nothing to report. From the second failure in a row, failures are reported the same way; `none` reports nothing. On update, `--deliver chat` needs `--channel` and `--chat` again. Project Agents cannot deliver to a chat.

`--watch` makes a `--cron` or `--every` job check something before each Run, without a Model, and start the Run only when the output changed since the last check: an `http://` or `https://` address is read as a page (its main text), anything else runs as a command in the host shell (PowerShell on Windows, bash elsewhere) in the Agent's Workspace or the Project's directory. The first check records the output without a Run; the Run receives what changed. A failing check counts like a failed Run. `--clear-watch` removes the watch.

Write self-contained prompts stating the work, relevant constraints and expected report. A Project-targeted job can block Project removal; retarget or delete it first.

```bash
vbot cron create assistant --name "Morning news" --prompt "Check the news and report relevant changes" --cron "0 9 * * *"
vbot cron create assistant --prompt "Check the build and report a changed result" --every 15 --repeat 4
vbot cron update <job-id> --status paused
```
