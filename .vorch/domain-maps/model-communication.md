# Model Communication

The sanctioned runtime channels through which the kernel informs the Model, and how to choose between them. When a producer in any domain (Extension, Channel, Tool, automation) needs to tell the Model something, it must use one of these channels - never invent a new shape, tag, or side path.

## Overview

Everything the Model sees at runtime arrives through one of seven channels. They differ on two axes: **durability** (persisted Session record vs request-time-only) and **trust** (kernel-authored constants vs quoted external content). Chat owns the request-rendering mechanics; producers only persist records or declare content through their own domain APIs.

The core term System Reminder lives in `.vorch/GLOSSARY.md`.

## Channels

| Channel | Durability | Owner / mechanics |
|---|---|---|
| Persisted note (`role: "note"`) | Session history | `chat.md` + `chat/request-building.md` - embedded into provider requests as a synthetic user message wrapped in `<system-reminder>` tags |
| Reply-surface reminder | Append-only tagged chronology | `chat/request-building.md` - appended by Chat at executor start; producers only pass the surface value |
| Speech-transcription reminder | Request-time only, hidden | `chat/request-building.md` - added when `input_origin="speech_transcription"` |
| Skill announcement | Once per Prompt Epoch | `skills.md` - tail note when a Skill becomes available+allowed |
| Continuation checkpoint reminder | Request-time only | `compaction.md` - ContinuationStrategy appends it to the active request |
| System Prompt blocks | Rendered per request | `prompts.md`; Tool-owned dynamic blocks via `ToolPromptBlockRegistry` (`tools.md`) |
| Tool definitions and results | Per call | `tools.md` - description plus structured result data |

Deliberately **not** a reminder channel: Channel-observed group chatter persists as attributed `[channel-message]` text and renders as explicitly untrusted JSON-quoted background context (`channels.md`). Never route untrusted third-party content through reminder framing.

## Decision rules

- Background event the next Run should know durably -> persisted note. Existing examples: `channel_send` outbound context, internal automation triggers.
- Standing guidance that follows configuration or the current catalog -> System Prompt block (allowlist-gated).
- Input-quality caveat tied to how a message was produced -> hidden request-time reminder declared as an explicit input field; never hand-built text.
- Per-call contract or feedback -> Tool description / result envelope.
- Untrusted external content -> ordinary user content under its domain's quoting/attribution rules, never kernel voice.

## Gotchas

- `<system-reminder>` is a protocol token owned by Chat's request builder. Producers never write the tags themselves.
- Provider adapters never receive `role: "note"`; embedding happens before wire translation (`providers.md`).
- Reminders are synthetic user messages: content must be kernel-authored constants, never raw external or user data without its domain's quoting rules.
- Notes are invisible in UI and public history - they are not a user-notification mechanism.
