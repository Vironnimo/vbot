# Telegram Channel Review Handoff

**Status:** Bugs 1–3 fixed (commits `830731c`, `d9d38ea`, `414e14b`, 2026-06-10) — Bug 4, Bug 5, M1–M4 still open · **Date:** 2026-06-10
**Scope reviewed:** `core/channels/` (channels.py, telegram.py, adapter.py), `core/tools/channel.py`,
`server/rpc/channel_methods.py`, runtime wiring in `core/runtime/runtime.py`, plus the relevant
parts of `core/runs/runs.py`, `core/chat/commands.py`, `core/chat/events.py`,
`core/automation/automation.py`. Specs read: `.vorch/specs/channels.md`,
`.vorch/specs/channels/telegram.md`.

## Overall assessment

The architecture is solid: adapter lifecycle with bounded restart/backoff, create/update rollback,
per-chat FIFO queues, and `Run.subscribe()` replays past events so there is **no** race between
`trigger_run` returning and the relay subscribing (verified in `core/runs/runs.py:251`). Test
coverage in `tests/core/channels/` is good. The findings below are ordered by severity.

---

## Bug 1 — `/handoff` via Telegram is silently swallowed — **FIXED** (`830731c`)

Resolution: `_handle_command_action` got a `case _:` replying "This command is not available from
Telegram channels yet." (minimal option; full channel handoff still depends on the same session
rotation `/new` is waiting on).

**Where:** `core/channels/telegram.py` → `_handle_command_action` (~line 696–729).

`CommandDispatcher.dispatch` (`core/chat/commands.py:113`) returns
`CommandAction(name="handoff", argument=...)` for `/handoff [agent-id]`. The Telegram adapter's
`match command_action.name:` only has cases for `"compact"`, `"new_session"`, and
`"retry_last_turn"` — no `"handoff"` case and no default case.

Flow: `_handle_dispatch_result` sees a `CommandAction`, calls `_handle_command_action` (which does
nothing for handoff), and returns `True` (= handled). Result: no Run, no reply, the user message
vanishes without any feedback.

**Fix options:** either implement handoff for channels (per PROJECT.md, `/handoff` triggers an
internal note-driven Run, creates a new session, auto-runs the receiving agent — but session
rotation from Telegram is the same problem `/new` has, see its "not available yet" reply), or at
minimum add a `case _:` that replies "this command is not available from Telegram channels". Do
not leave the silent fall-through.

## Bug 2 — Edited messages trigger new Runs — **FIXED** (`d9d38ea`)

Resolution: handlers are ANDed with `filters.UpdateType.MESSAGE` (singular). **Correction to the
fix suggestion below:** `filters.UpdateType.MESSAGES` also matches `edited_message` in PTB 22.7
(verified empirically) — `MESSAGE` is the filter that matches only new messages. Edits and channel
posts are now ignored by design (spec updated).

**Where:** `core/channels/telegram.py` → `start()` handler registration (~line 110–117).

`MessageHandler(filters.TEXT, ...)` and `MessageHandler(filters.PHOTO | filters.Document.ALL, ...)`
match **all** update types that carry an `effective_message`. Verified against the installed
`python-telegram-bot` 22.7:

- `filters.TEXT` matches `edited_message` → **True**
- `filters.TEXT` matches `channel_post` → **True**
- (channel posts are currently dropped only by accident: `_conversation_facts` requires
  `effective_user`, which channel posts lack)

Consequence: a user editing an old message in an allowed chat triggers a brand-new Run with the
edited text. For media, editing a caption re-downloads the file and triggers a duplicate Run.

**Fix:** AND the filters with `filters.UpdateType.MESSAGES` (matches only `message`, excluding
edited/channel-post variants), e.g. `filters.TEXT & filters.UpdateType.MESSAGES`. Decide and spec
whether edits should ever be processed (currently the spec is silent on edits).

## Bug 3 — `/compact` and `/retry` block the entire update pipeline (and defeat `/stop`) — **FIXED** (`414e14b`)

Resolution: per the "move work into the per-chat queue" direction. Command actions are enqueued as
`_QueuedCommandAction` and executed by the per-chat worker (`CommandHandled` replies like `/stop`
stay eager). Media handlers no longer download in the handler: raw messages are enqueued as
`_QueuedInboundMedia` and downloaded in the worker. Album buffering stores raw messages and the
500 ms flush window restarts per buffered item (`_ALBUM_FLUSH_SECONDS`). Spec updated accordingly.

**Where:** `core/channels/telegram.py` → `_handle_inbound_message` eager dispatch path →
`_handle_command_action` (~line 696–729); also `_handle_inbound_media` → media download
(~line 308–355, `_build_media_message_blocks` / `_store_inbound_attachment`).

PTB's `Application` processes updates **sequentially** by default (`concurrent_updates=False`,
which is what `Application.builder().token(...).build()` produces). The per-chat queue design
keeps normal messages fast in the handler (route + enqueue only), but two paths do long awaits
*inside the handler*:

1. `_handle_command_action("compact")` awaits `TriggerService.compact_session()` (a model call).
2. `_handle_command_action("retry_last_turn")` awaits `_relay_run_events(run, ...)` — i.e. the
   **entire Run** until a terminal event.

While either runs, the bot processes **no further updates for any chat on this adapter**. The
spec's stated purpose of eager dispatch ("cancellation can be handled while a previous queued
message is waiting on a Run", `.vorch/specs/channels/telegram.md`) is defeated: a `/stop` sent to
cancel a `/retry`-relayed Run is never even read until that Run finishes.

Related, same root cause:

- **Media downloads block the pipeline.** `_handle_inbound_media` awaits
  `bot.get_file(...)` + `download_as_bytearray()` in the handler before enqueueing. A slow/large
  document stalls all chats.
- **Albums split.** Album items are buffered only *after* their download completes, and PTB feeds
  the handler sequentially — so item N+1's download starts after item N's. The 500 ms flush window
  in `_flush_album` starts at the first buffered item; any cumulative download time > 500 ms splits
  one album into multiple Runs (`test_album_messages_are_buffered_into_single_trigger_run` passes
  because tests stub the download as instant).

**Fix direction:** move the slow work off the handler. Options: run `compact`/`retry` actions as
fire-and-forget tasks (mirroring how `_relay_run_events` already runs inside the per-chat worker
for normal messages — e.g. route command actions through the per-chat queue but keep
`CommandHandled` replies eager), and download media inside the per-chat worker or a background
task instead of the handler. Alternatively enable `concurrent_updates`, but that changes ordering
guarantees globally — the per-chat queue already exists precisely to own ordering, so moving work
into it is the more consistent fix. The album flush window should start counting from the *last*
buffered item (reset timer per item) rather than the first, regardless.

## Bug 4 — Caption limit (1024) not handled on outbound file sends

**Where:** `core/channels/telegram.py` → `send()` (~line 150–171), `_send_single_file`,
`_send_homogeneous_batch`.

When `files` are present, the full `message` is attached as the caption of the first file. Telegram
caps captions at **1024** characters (text messages: 4096). `split_telegram_message` is only
applied on the text-only path. A `channel_send` with a file plus >1024 chars of text fails with
`telegram.error.BadRequest`.

Compounding it: `_handle_channel_send_tool` (`core/tools/channel.py:93`) only catches the
`ChannelError` family + `ValueError` — a PTB `BadRequest` escapes as an unexpected error, so the
agent gets a raw exception instead of a clean `tool_failure`.

**Fix:** if the message exceeds the caption limit, send files with no caption (or a truncated one)
and deliver the text via the normal `send_message` split path; and/or wrap PTB errors at the
adapter boundary into `ChannelError` so tool/relay callers handle them uniformly.

## Bug 5 — `split_telegram_message` counts code points, Telegram counts UTF-16 units

**Where:** `core/channels/telegram.py` → `split_telegram_message` (~line 851–857).

The Bot API's 4096 limit is measured in **UTF-16 code units**; astral-plane characters (most emoji)
count as 2. Python slicing counts code points, so a 4096-code-point chunk that is emoji-heavy can
exceed the wire limit → `BadRequest` → the reply is lost (relay exceptions are only logged in
`_run_chat_queue`; the user gets nothing).

**Fix:** split by UTF-16 length (`len(text.encode("utf-16-le")) // 2` per chunk, without splitting
inside a surrogate pair / grapheme), or simply use a conservative chunk size measured in UTF-16
units. Low frequency, but a messaging bot replying with emoji makes it plausible.

---

## Minor findings

### M1 — Proactively created sessions get no channel metadata

Inbound routing (`_prepare_inbound_route`) writes `source_channel_id`, `platform`,
`platform_conv_id`, `last_reply_target` via `_update_session_metadata`. The outbound path
(`ensure_outbound_session` → `_ensure_channel_session`) writes only the one-time reminder note —
no sidecar metadata. Consequences: a session created purely by `channel_send` is not recognizable
as a channel session in the WebUI, and has no `last_reply_target` until the first inbound message
arrives. Inconsistent with the metadata contract in `.vorch/specs/channels.md` ("Constraints &
Gotchas", last bullet).

### M2 — Voice/audio/video/sticker messages are ignored without feedback

Only `filters.TEXT` and `filters.PHOTO | filters.Document.ALL` handlers are registered. Voice
messages, audio, video, video notes, and stickers fall through with no handler — no Run, no user
feedback. Voice in particular is a gap given vBot has STT infrastructure
(`input_origin: "speech_transcription"` already exists in the chat layer). At minimum a polite
"unsupported content type" reply would help; STT ingestion is the obvious feature follow-up.

### M3 — One corrupt `channel.json` prevents server startup

`ChannelStorage.load_all()` (`core/channels/channels.py:146`) raises on the first invalid config;
`ChannelService.start()` only try/excepts around `start_channel(...)` *inside* the loop, not around
`load_all()` itself; `Runtime._start_channel_service()` (`core/runtime/runtime.py:551`) does not
catch either → `runtime.start()` fails → server does not boot.

This is a tension, not necessarily a bug: PROJECT.md mandates fail-fast validation for user-editable
JSON, but `.vorch/specs/channels.md` says "Runtime startup degrades per channel … does not prevent
the server from starting" (that sentence currently only holds for *runtime dependency* failures
like a missing token, not config-schema failures). Given agents self-configure and restart the
server, "one bad channel file = server won't boot" is a risky failure mode. Decide which behavior
is intended and align code + spec; per-channel degradation with a failed-channel diagnostic would
match the rest of the channel design better.

### M4 — Duplicated channel reminder template

`_SYSTEM_REMINDER_TEMPLATE` in `core/channels/telegram.py` (~line 48) and
`_channel_system_reminder()` in `server/rpc/channel_methods.py` (~line 275, used by
`session.link_channel`) are two independent copies of the same text. They will drift. Move to one
shared definition (e.g. in `core/channels/adapter.py` or `channels.py`).

---

## Explicitly checked and found OK (don't re-investigate)

- **No subscribe race:** `Run.subscribe()` replays buffered events (deque maxlen 4096) before
  streaming; runs start in `RUNNING`, terminal-state subscribe replays and exits. The Telegram
  relay consumes without awaits in the loop body, so lagging-subscriber eviction is not a realistic
  concern here.
- **Queueing:** `TriggerService.trigger_run` falls back to `queue_run` + awaits the future on
  `ActiveRunError`; combined with per-chat worker serialization this is correct, including the
  dm_scope=`main` case where multiple chats share one session.
- **Lifecycle:** stop/cancel paths (`_run_adapter` finally → `adapter.stop()`,
  `_stop_chat_workers`, stop-task bookkeeping, pending-start-after-stop, restart backoff and
  failed-marking, create/update rollback) all line up; covered by tests.
- **Session-id derivation:** inbound (`_derive_session_id`) and outbound
  (`_conversation_facts_for_target`) agree, including the private-chat `chat_id == user_id`
  assumption and negative-group-id handling.
- **Eager `/stop` works** for runs started via the normal message path (worker-relayed) —
  the test `test_stop_command_is_eagerly_dispatched_while_chat_worker_is_blocked` covers this.
  It is only retry/compact-initiated work that blocks the pipeline (Bug 3).
- `/handoff` matching in `core/chat/commands.py:117` correctly uses `"/handoff"` (an earlier grep
  rendering made it look like `"\handoff"` — it is not).

## Suggested fix order

1. Bug 1 (`/handoff` swallowed) — small, isolated, add tests for unknown/unsupported actions.
2. Bug 2 (edited messages) — one-line filter change + tests.
3. Bug 3 (pipeline blocking) — the largest change; decide the design (move work into per-chat
   workers) before coding; update `.vorch/specs/channels/telegram.md` accordingly.
4. Bug 4 + Bug 5 (outbound limits) — one shared "Telegram send hygiene" pass: caption overflow,
   UTF-16 chunking, PTB-error wrapping at the adapter boundary.
5. M1–M4 as follow-ups; M3 needs a product decision (fail-fast vs degrade).

After each fix, update `.vorch/specs/channels/telegram.md` (and `channels.md` for M1/M3) — several
spec sentences describe the buggy behavior as intended (e.g. eager command dispatch rationale,
album 500 ms window).
