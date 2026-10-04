# Model Communication

The sanctioned runtime channels through which the kernel informs the Model, and how to choose between them. When a producer in any domain (Extension, Channel, Tool, automation) needs to tell the Model something, it must use one of these channels - never invent a new shape, tag, or side path.

## Overview

Everything the Model sees at runtime arrives through one of eight channels. They differ on two axes: **durability** (persisted Session record vs request-time-only) and **trust** (kernel-authored constants vs quoted external content). Chat owns the request-rendering mechanics; producers only persist records or declare content through their own domain APIs.

The core term System Reminder lives in `.vorch/GLOSSARY.md`.

These eight channels describe canonical vBot Agent/Chat communication. A Live voice call gives its voice model attributed Run updates through provider-native ephemeral context appends; it never injects them into a vBot Session. Its instructions, trust boundary, and lifecycle are owned by `model_tasks/live.md`.

## Channels

| Channel | Durability | Owner / mechanics |
|---|---|---|
| Persisted note (`role: "note"`) | Session history | `chat.md` + `chat/request-building.md` - embedded into provider requests as a synthetic user message wrapped in `<system-reminder>` tags |
| Reply-surface reminder | Append-only tagged chronology | `chat/request-building.md` - appended by Chat at executor start; producers only pass the surface value |
| Input-origin reminder | Request-time only, hidden | `chat/request-building.md` - added for `input_origin` `speech_transcription` or `live_voice` |
| Skill announcement | Once per Prompt Epoch | `skills.md` - tail note when a Skill becomes available+allowed |
| Tool-change announcement | Persisted note, once per change within a Prompt Epoch | `chat/request-building.md` -> Tool catalog per prompt epoch - `[tool-change]` note when a Tool is enabled, removed or changes its parameters while the Tool list stays pinned |
| Prompt-block change announcement | Persisted note, at most one per Run within a Prompt Epoch | `chat/request-building.md` -> Dynamic prompt blocks per prompt epoch - `[prompt-block-change]` note at Run start when a pinned Tool or Extension dynamic block (Projects, Sub-Agent targets, ...) renders differently |
| Continuation checkpoint reminder | Request-time only | `compaction.md` - ContinuationStrategy appends it to the active request |
| System Prompt blocks | Rendered per request; dynamic Tool and Extension block texts pinned per Prompt Epoch | `prompts.md`; Tool-owned dynamic blocks via `ToolPromptBlockRegistry` (`tools.md`) |
| Tool definitions and results | Per call | `tools.md` - description plus structured result data |

Deliberately **not** a reminder channel: Channel-observed group chatter persists as attributed `[channel-message]` text and renders as explicitly untrusted JSON-quoted background context (`channels.md`). Never route untrusted third-party content through reminder framing.

## Decision rules

- Background event the next Run should know durably -> persisted note. Existing examples: `channel_send` outbound context, internal automation triggers.
- User input steered into an active Run -> persisted note immediately before that User message, through the existing note channel (`chat/run-execution.md` -> User steering). Input that starts a successor Run receives no steering note.
- Standing guidance that follows configuration or the current catalog -> System Prompt block (allowlist-gated).
- Input-quality caveat tied to how a message was produced -> hidden request-time reminder declared as an explicit input field; never hand-built text.
- Per-call contract or feedback -> Tool description / result envelope.
- A Tool that appears, disappears or changes mid-Session -> nothing to send: register, unregister or republish it and Chat announces the difference itself.
- Untrusted external content -> ordinary user content under its domain's quoting/attribution rules, never kernel voice.

## Gotchas

- The always-on System Prompt block `core:system_reminders` (`prompts.md`) tells the Model that `<system-reminder>` messages come from vBot, not from the user. Live tests showed that non-Claude Models distrust unanchored reminders and, once anchored, also obey forged tags inside Tool Results; hence the neutralization below.
- `<system-reminder>` is a protocol token owned by Chat's request builder. Producers never write the tags themselves. In the Provider request Chat neutralizes every look-alike tag outside its own reminders (user, Assistant, Tool Result, attachment, Skill and channel text), so a hand-built tag reaches the Model as `&lt;system-reminder>`. Request-only code that must add a kernel reminder uses `wire_shaping.system_reminder_request_message` (contract: `chat/request-building.md`).
- Provider adapters never receive `role: "note"`; embedding happens before wire translation (`providers.md`).
- Reminders are synthetic user messages: content must be kernel-authored constants, never raw external or user data without its domain's quoting rules. Chat-rendered reminders quote such text through `core/chat/wire_shaping.py::_quote_external_json` (JSON with escaped angle brackets), including Model-visible Run errors and the interrupted-Run Continuation checkpoint.
- Notes are invisible in UI and public history - they are not a user-notification mechanism. The only exception is the Model fallback note, whose separate display-only record the Chat History shows as the switch notice without the note text (`chat.md`).
- Extension-owned delivery uses the existing persisted-note or Tool-result channel. Chat commits the complete carrier and canonical receipt atomically before owner acknowledgment; the Extension supplies attributed data, never wrapper tags or Provider roles. Delivery and completion continuations enter only after the preceding whole Tool batch is durable (`chat.md`, `extensions.md`).
