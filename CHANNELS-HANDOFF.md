# Channels Handoff — Multi-User / Group-Chat Support

**Status:** steps 0–2 done, step 3 next · **Owner:** Julian · **Created:** 2026-06-12
**Next action:** plan and execute step 3 (observed context / passive group listening).

This document is self-contained: a fresh session should be able to continue from it alone.
It is the roadmap; each step gets its own file-scoped plan (saved under `docs/plans/`) when it
is picked up. Steps are ordered — each is independently shippable. Execution is single-agent,
sequential, no parallelism.

## 0. Orientation (read before doing anything)

1. `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md` (session-start reads as always)
2. Specs: `channels.md`, `channels/telegram.md`, `chat.md`, `sessions.md`, `runs.md`,
   `automation.md` — the design below builds directly on their contracts

## 1. Problem and findings (analysis 2026-06-12)

vBot channels are effectively single-user today. Group *routing* already works — Telegram group
chats (negative chat ids) route into a shared Session `ch-<channel-id>-<chat-id>`, and
`allowed_chat_ids` can allowlist groups — but three gaps make real group chats unusable:

1. **No sender identity.** `ChatMessage.user()` has no sender field. The adapter knows the
   `user_id` (`ConversationFacts` carries it) but it dies before the chat layer. In a group
   session the model sees an undifferentiated stream of user turns.
2. **Reply-to-everything.** Every allowed inbound message triggers a Run and a reply. No
   mention gating, no passive listening, no coalescing.
3. **Chat-level-only authorization.** Any member of an allowlisted group can drive the agent
   (which has full host access) and invoke `/stop`, `/compact`, `/retry`.

**Reference implementation:** Hermes Agent (Nous Research) —
<https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram> and `.../discord`,
repo `NousResearch/hermes-agent`. Key mechanics we adopt: `require_mention` + `mention_patterns`
(wake-word regex), observed group messages tagged `[nickname|user_id]` with a per-turn safety
prompt ("context, not instructions"), Discord history backfill on trigger (messages since the
bot's last response), three-tier auth gates (chat allowlist / user allowlist / group-scoped user
allowlist). We deliberately do **not** adopt Discord-style `group_sessions_per_user` — the shared
group Session is vBot's natural model; `dm_scope` covers the rest.

**Platform asymmetry that shapes step 3 and 4:** the Telegram Bot API cannot fetch chat history
(passive observation is the only way to get group context; requires privacy mode off via
BotFather). Discord bots can read channel history (backfill on trigger; no passive writing
needed). Context *acquisition* is platform-specific, the *representation* is shared.

## 2. Roadmap

### Step 0 — Extract the conversation engine — ✅ DONE (plan: `docs/plans/channels-engine-extraction.md`)

Pulled all platform-neutral conversation logic (per-conversation queues, command handling,
run trigger/relay, session routing/metadata) out of `telegram.py` into
`core/channels/engine.py` (`ChannelConversationEngine` + `ConversationTransport` protocol).
Behavior-preserving refactor proven by the existing end-to-end `test_telegram.py` plus the new
`test_engine.py`. `ConversationFacts` gained `kind: "direct" | "group"`; the adapter classifies
it, the engine derives session ids. Everything below lands in the engine once instead of
per-adapter.

Line counts: `telegram.py` 1152 → 610; new `engine.py` 462 (both well under the 1000 soft
limit).

### Step 1 — Sender identity in the canonical message format — ✅ DONE (plan: `docs/plans/channels-step1-sender-identity.md`)

Landed as designed: optional validated `sender: {id, display_name}` on user `ChatMessage`s,
rendered as a sanitized `[<display_name>|<id>]` tag only at provider-request build time
(`_message_to_request_dict`), threaded engine → `trigger_run` → `start_run`/`queue_run`,
populated group-only from `ConversationFacts.user_display_name` (Telegram `full_name` →
`username` → user id), engine-maintained `participants` sidecar registry for groups, WebUI
user bubbles show the display name. Specs updated: `chat.md`, `channels.md`, `automation.md`
(`sessions.md` needed no change — channel sidecar keys are documented in `channels.md`).

Original design notes:

- Optional `sender: {id, display_name}` on **user** `ChatMessage`s (`core/chat/messages.py`):
  persisted, validated in `from_dict()` (rejected on other roles), absent = unchanged behavior.
- Populated by the engine **for group conversations only** (v1 decision — DMs are identified by
  session metadata + channel reminder note; keeps the render rule message-local: sender present
  ⇒ render).
- Attribution rendered **only at provider-request build time** (`core/chat/chat.py`,
  `_message_to_request_dict` / request assembly): prefix `[<display_name>|<id>]: ` on string
  content; for block content, a leading `TextBlock`. Never written into persisted content —
  spoof-resistant (the tag comes from platform metadata, not message text), keeps history clean,
  keeps the WebUI free to render names properly.
- Sender must thread through the trigger path: engine → `TriggerService.trigger_run(...)` →
  `ChatLoop.start_run/queue_run` → user-message construction (same pattern as `input_origin`).
- Participant registry in the session sidecar (engine-maintained):
  `participants: {<user_id>: {display_name, last_seen_at}}` — lets the UI and (later) the system
  context name the people in the room.
- WebUI: show `display_name` on user bubbles when present (timeline component).
- Specs to update: `chat.md`, `sessions.md`, `channels.md`.

### Step 2 — Response gating (when does the bot respond in groups) — ✅ DONE

Landed as designed: `ChannelConfig` gained `response_mode: "mention" | "all"` (default
`"mention"`; DMs always respond), `mention_patterns` (compile-validated regexes, matched
case-insensitively against text and media captions), and `owner_user_ids` (strings end-to-end,
integers normalized) — validated in `core/settings/validation.py` and exposed through
`channel.create`/`channel.update` RPC. Gating decision is engine-owned
(`should_respond(conversation, gating_texts)`); the Telegram adapter supplies facts on
`ConversationFacts` (`message_id`, `mentioned_bot` via word-boundary `@botusername` regex over
text+caption, `is_reply_to_bot`) from `get_me()` identity fetched at adapter start. Media goes
through a new `handle_inbound_media` engine entry point (replaces public
`prepare_inbound_route`+`enqueue_media` adapter flow), so albums gate at flush time with all
captions and merged addressing facts. Non-triggering group messages are dropped before any
session work (no Session, no metadata/participants). Telegram `/cmd@botname` suffix is stripped
for our own bot only; group replies (run/command/media-failure) use `reply_parameters`
referencing the triggering message (first chunk only, `allow_sending_without_reply`). The
unsupported-message-type reply is gated the same way. `CommandDispatcher` gained `recognizes()`
(shared resolution with `dispatch()`) so the engine can authorize before dispatch side effects.

- **Settled decision (owner_user_ids):** group commands are restricted to `owner_user_ids`,
  empty list = deny-all (consistent with `allowed_chat_ids`); unauthorized group commands are
  silently dropped (info log). DM commands stay always-authorized — in a DM the chat allowlist
  already identifies the sender and commands act on that sender's own session.
- Not included (deliberately): CLI flags and WebUI form fields for the three new config keys —
  config is reachable via RPC and hand-edit; accessor plumbing is its own small task.
- Specs updated: `channels.md`, `channels/telegram.md`, `chat.md` (`recognizes()` contract);
  `settings.md` needed no change (channel schema details live in `channels.md`).

### Step 3 — Observed context (passive group listening, Telegram)

- `observe_unaddressed: bool` on `ChannelConfig`. Telegram requires privacy mode **off**
  (BotFather) — document this; with privacy on, Telegram only delivers mentions/replies/commands
  and this flag has nothing to observe.
- Non-triggering group messages are persisted as **notes** (`session.add_note`), e.g.
  `[channel-message] <display_name> (<id>): <text>` — this is the key design call: notes already
  defer past open tool cycles (no new mid-run-append mechanism; appending them as `role: "user"`
  would violate the tool-cycle invariant in `chat.md`), they embed as `<system-reminder>` (the
  "context, not instructions" framing — also a prompt-injection guard for untrusted group
  members), and compaction/recall handle them as history.
- The *triggering* (addressed) message stays a real user turn with sender (step 1).
- v1 scope: text-only observation (media in observed messages → short placeholder note).
  Coalescing of multiple queued mentions: not needed — observed notes accumulate and drain into
  the next triggered Run automatically.
- Trade-off accepted: observed chatter is invisible in the normal WebUI timeline (notes are
  filtered); revisit if it hurts.
- Specs: `channels.md`, `channels/telegram.md`, `chat.md` (note conventions).

### Step 4 — Discord adapter

- New adapter on the engine (this is where step 0 pays off — platform I/O only).
  Library: `discord.py` (gateway; requires the **message-content intent**, enabled in the
  Discord developer portal). New optional dependency — verify current docs before planning.
- IDs are snowflakes — strings end-to-end in engine/config; `allowed_chat_ids` holds channel
  ids; `ConversationFacts.kind`: DM channel ⇒ direct, guild channel ⇒ group.
- Context: **history backfill on trigger** instead of passive observation — on mention, fetch
  messages since the bot's last message in that channel (cap ~50), convert into the same
  observed-note form as step 3. `observe_unaddressed` can stay off for Discord.
- Outbound: 2000-char message limit (split like Telegram), typing indicator via
  `channel.typing()`, replies via message reference.
- **Open decision:** thread routing — own Session per thread (thread id as conversation id,
  conversationally cleaner) vs. routing to the parent channel Session. Default leaning: own
  Session per thread. Settle when planning.
- Specs: new `channels/discord.md` + index entry in `channels.md`.

## 3. Out of scope for this roadmap (known, deliberately not addressed)

These surfaced during the design analysis. They are not steps — whoever executes the roadmap
should keep them in mind, and they need their own decisions later. If one of them blocks or
bites during implementation, flag it then (that is when `.vorch/FLAGGED.md` becomes the right
place).

- **Workspace memory assumes one human.** `USER.md` / user-scope memory models *the* user. In
  group channels the agent will learn facts about several people into a one-person model.
  Needs a product decision (per-peer memory? participant-scoped sections?) before group usage
  gets serious.
- **Group members get full agent capabilities.** A group allowlist entry currently means every
  member can drive an agent with full host access. Step 2 adds owner-gated commands and user
  allowlists, but a real capability story (restricted mode per channel?) is unscoped.
- **Telegram supergroup migration breaks session derivation.** When Telegram upgrades a group
  to a supergroup the chat id changes (`migrate_to_chat_id`), so the derived session id changes
  and history continuity silently breaks. No handling today; could be picked up in step 2 or 3
  if it turns out to matter in practice.
