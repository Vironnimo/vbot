# Cron email jobs

Read when changing Cron jobs of schedule type `email`: the trigger and its filters, how an email Channel hands a mail to Cron, the mails waiting for a Run, the email part of the Run's context note, and the surfaces that create such jobs. User decision 2026-10-28: the mailbox is a Channel; email jobs reference it and filter by sender and subject; mail reaches the Run as data labeled untrusted, with no Tool ceiling of its own. Rules every Cron job shares (execution health, delivery, failure alerts): `cron.md`. The Channel side (IMAP/POP3 polling, which mails a conversation takes, `InboundMail`): `../channels.md`.

## Modules

- `core/automation/_cron_email.py`: `CronEmails` (the service's `_emails`) accepts mails and keeps those waiting; `WaitingMail` is one stored mail; `email_note` builds the Run's note.
- `core/automation/_cron_inbox.py`: `CronInbox`, the storage, caps, dedupe window and batch hand-over email and webhook jobs share (`cron-webhooks.md` -> Waiting requests).
- `core/automation/_cron_jobs.py`: `CronEmailTrigger` (`channel_id`, `senders`, `subject`) and the job field `email`; `_cron_delivery.check_email_channel` checks the Channel.
- `CronService` interface: `accept_mail(mail)` (the `InboundMailSink` of `core/channels/mail.py`; Runtime binds it with `ChannelService.set_mail_sink` in `core/runtime/_bootstrap.py`), `email_status(job)` (`InboxCounts`, None for other schedules).

## Trigger and validation

- `email` `{channel_id, senders?, subject?}` is stored only on an `email` job; any other schedule clears it, and an `email` schedule without it is refused (`an email schedule needs email.channel_id: the email Channel`). It is persisted additively in Generation 1 `jobs.json`.
- `senders`: at most 20 full addresses or `@domain` suffixes, trimmed, lowercased and deduplicated; a sender matches an address exactly or a suffix by `endswith`, so `@example.com` does not match `x@example.com.evil.net`. `subject`: at most 200 characters, matched as a case-insensitive substring. An omitted filter matches every mail.
- On create, and on an edit that changes `email`, `agent_id` or `schedule_type`, the Channel must exist, have platform `email` and belong to the job's Agent (`check_email_channel`); a Project job is refused (`a job of a Project Agent cannot run on email: Channels belong to Identity Agents`). Without Channels (`set_chat_delivery` never bound) creating one is refused.
- An email job has no time: no `next_fire_at`, no calendar projection, no late notice, and it refuses `watch`. `remaining_runs` counts Runs like a webhook job's.

## Accepting a mail

- The Channel calls `accept_mail` for each mail no conversation takes. It returns once every active email job of that Channel whose filters match holds the mail durably; a paused job takes none. It raises `CronStorageError` when the jobs cannot be read or a job could not store the mail (after trying every job), so the Channel hands the mail over again; jobs that stored it already take it once.
- A job takes each `message_id` at most once: the state file keeps the last 1024 keys (`INBOX_MAX_KEYS`), also after the mail's Run. A mail the Channel hands over again after a restart or failure starts no second Run.
- State: `cron/email-events/<sha256 of job id>.json`, `{job_id, next_seq, last_received_at, dropped, mails: [...], keys}`; each mail keeps `received_at`, `folder`, `sender`, `sender_name`, `sender_authenticated`, `recipients`, `subject`, `sent_at`, `text` (at most 16,000 characters, `EMAIL_TEXT_MAX_CHARS`), `text_cut` and attachments as name, type and size. Runtime state outside the JSON document contract, with the read-failure and retry rules of webhook requests.
- At most 10 mails and 16,000 characters wait; the oldest are dropped and counted. Leaving the email schedule, switching to another Channel, or deleting the job discards the waiting mails.
- The job task and Run hand-over are the shared inbox path (`_cron_scheduler._run_inbox_job`, `_cron_execution._trigger_job_run(inbox=True)`), the same as webhook requests.

## The Run

- `_cron_context.run_context` puts the email note after any event, late or change part and before the delivery note: which Channel received how many matching mails, that the records are data and not instructions, that `sender_authenticated: false` means the address can be forged, how many earlier mails were dropped unseen, that attachments are listed only, and which texts are cut; then one JSON record per mail, oldest first, pretty-printed with `<` and `>` escaped. Records hold `received_at` and `sent_at` in local time; `message_id` is not shown. Wording and reasons: `../tools/cron.md` -> Agent-facing text.
- The INFO `Cron job fired` line ends in `email_mails=<n> email_dropped=<n>`. The second-failure alert says the job keeps running when its email Channel receives a matching mail.
- No Tool ceiling limits an email Run (user decision 2026-10-28); Agent permissions and a job's persisted permission ceiling apply as for other jobs.

## Logging

No log line carries an address, subject or mail text. ERROR `Cron mail not stored; the Channel hands it over again (job=... channel=...)`; DEBUG `Cron mail stored (job=... channel=... waiting=... dropped=...)` and `Cron mail taken by no email job (channel=...)`; the shared inbox WARNING/ERROR lines name `mails` (`Waiting mails could not be read; retrying without replacing them.`).

## Surfaces

RPC `cron.create`/`cron.update` take `email` `{channel_id, senders?, subject?}` (`update` replaces it whole), and every job payload carries `email` with `last_received_at`, `waiting`, `dropped` and `error?` (null for other schedules). The `cron` Tool takes `schedule: "email"` with `email_channel`, `email_from` and `email_subject` and shows `last_mail` and `mails_waiting` (`../tools/cron.md`); the CLI takes `--email <channel-id>`, repeatable `--email-from`, `--email-subject` and on update `--clear-email-from` / `--clear-email-subject` (`../cli/agent-commands.md`); the WebUI offers the frequency `On each new email` with an email Channel picker and both filters (`../webui/source-map.md`).

## Coverage

`tests/core/automation/test_cron_email.py` (stored once, escaped records, filters, paused job, dedupe after the Run, caps across a restart, a failed store, validation and leaving the schedule), `tests/server/rpc/test_automation_methods.py`, the Tool tests (`test_cron.py`, `test_cron_call_tolerance.py`), `tests/cli/test_cli_cron.py`, WebUI `cronView.test.js`, `CronView.test.js`.
