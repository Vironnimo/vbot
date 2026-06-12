# Telegram Channel Review Handoff

**Status:** All findings resolved. Bugs 1–3: `830731c`, `d9d38ea`, `414e14b` (2026-06-10).
Bugs 4–5 and M1, M3, M4: 2026-06-12. M2 resolved separately (voice/audio/video handlers +
unsupported-type reply now exist) · **Date:** 2026-06-10 (updated 2026-06-12)
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

## Bug 4 — Caption limit (1024) not handled on outbound file sends — FIXED (2026-06-12)

**Resolution:** `send()` now routes file sends through `_send_with_files`: a message within
`TELEGRAM_CAPTION_LIMIT` (1024 UTF-16 units) still rides as the first file's caption, a longer
message is sent as standalone text first and the files go out uncaptioned. Both outbound entry
points (`send`, `send_text`) run inside `_telegram_error_boundary`, which wraps
`telegram.error.TelegramError` (incl. `BadRequest`) into `ChannelError` so `channel_send` returns
a clean `tool_failure`.

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

## Bug 5 — `split_telegram_message` counts code points, Telegram counts UTF-16 units — FIXED (2026-06-12)

**Resolution:** `split_telegram_message` now accumulates chunks by UTF-16 length (`_utf16_units`:
2 for astral-plane characters, 1 otherwise) and only breaks at character boundaries, so no chunk
exceeds the wire limit and surrogate pairs are never split.

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

### M1 — Proactively created sessions get no channel metadata — FIXED (2026-06-12)

**Resolution:** `engine.ensure_channel_session` now also calls `_update_session_metadata` with
`track_participant=False`, so the outbound path writes the base sidecar keys (`source_channel_id`,
`platform`, `platform_conv_id`, `last_reply_target`) but records no fake participant.

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

### M3 — One corrupt `channel.json` prevents server startup — FIXED (2026-06-12)

**Resolution:** `ChannelStorage.load_all()` now skips a config that fails to parse/validate with a
logged warning instead of raising, so one bad file blocks neither server startup nor the other
channels. Strict single-channel access (`get`) still raises. Per-channel degradation is now the
intended behavior for config-schema failures too (spec `channels.md` updated).

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

### M4 — Duplicated channel reminder template — FIXED (2026-06-12)

**Resolution:** one shared builder `channel_system_reminder()` now lives in
`core/channels/adapter.py` (exported from `core.channels`). The engine and `session.link_channel`
both call it; the duplicated `_SYSTEM_REMINDER_TEMPLATE` (engine) and `_channel_system_reminder`
(server) copies are gone.

(Note: the template had since moved from `telegram.py` to `engine.py`; the server copy lived in
`channel_methods.py`.)

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
