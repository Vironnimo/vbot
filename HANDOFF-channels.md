# Channel Reply-Destination Handoff

**Status:** Discovery checkpoint. This topic is not implementation-ready. Do not turn the conceptual direction below into code until the open product and technical decisions have been discussed with the user.

**Last updated:** 2026-07-12

## Why This Handoff Exists

An Agent generated an image during a Telegram-originated Run and returned WebUI image Markdown instead of sending the file through `channel_send`. The initial fix clarified that every Channel file delivery must use `channel_send`, but the follow-up discussion exposed a deeper problem: the Agent does not reliably know the reply destination of the current Run when one Session is used from multiple accessors.

The discussion repeatedly reached a conceptual direction before the important consequences and implementation decisions were surfaced. This document intentionally preserves uncertainty. It records what is verified, what the user has actually decided, what was rejected, what remains open, and which failure modes must be addressed before implementation.

## User Goal

The Agent should know enough about the current reply destination to choose the correct delivery behavior without being flooded with repetitive reminders. A normal text reply should follow the accessor's automatic reply path. A file sent through any Channel must use `channel_send` with `file_paths`. WebUI and Desktop currently have the same relevant rendering behavior and may be treated as `WebUI` for this capability.

When a Session continues through the same reply destination, the Agent should not receive the same reminder on every Run. When the reply destination changes, the Agent should receive a short, clear notice before answering the message that caused the change.

## Communication Requirements for Continuing This Discussion

- Never present the current conceptual direction as if the design were complete or implementation-ready.
- For every proposal, explain the verified current behavior, the proposed behavior, the difference between them, expected negative consequences, alternatives, and unresolved decisions.
- Surface problems and opportunities to improve the user's idea; do not merely agree and optimize for the smallest next task.
- Agent-facing wording must be shown as exact before/after text before it is changed. Keep that wording short and unambiguous.
- Separate confirmed user decisions from recommendations and from unresolved working assumptions.

## Verified Current System

### Accessor identity

- Browser WebUI and Desktop can distinguish themselves in the frontend. Desktop loads the same WebUI inside pywebview with `?accessor=desktop`.
- The frontend sends `browser` or `desktop` only on the persistent WebSocket connection used for the connected-clients roster.
- The normal `chat.stream` request does not carry the accessor type or the window connection id. It sends Agent, Session, content, optional file mentions, and optionally `input_origin`.
- `input_origin` currently means only `speech_transcription`. It is orthogonal to reply destination: a transcribed message can still originate from WebUI/Desktop, and it must not be overloaded with accessor routing.
- Consequently, an ordinary Run cannot currently distinguish browser WebUI from Desktop. For the behavior in scope, that distinction is not needed because both use the same Markdown rendering path.

### Channel sessions

- Telegram and Discord create or resolve normal Sessions and route their inbound messages through the shared chat loop.
- A new Channel Session receives one persisted system-reminder note identifying the platform, Channel id, and platform chat id.
- Channel metadata such as `source_channel_id`, `platform`, `platform_conv_id`, and `last_reply_target` describes a stable Channel binding and the target available to `channel_send`; it does not prove that the current Run originated from that Channel.
- The Channel engine owns the actual reply plan for an inbound Channel message and automatically relays only the final assistant text. Files and tool artifacts are not part of that relay.
- A Telegram Session can be opened and continued normally from WebUI. WebUI displays the Channel identity in the Session drawer, but sending from that selected Session uses the same `chat.stream` request as any other WebUI message. No fresh WebUI-origin notice is added for the Agent.
- Therefore, when a Channel-bound Session is continued from WebUI, the Agent still sees the old persisted Telegram note and can incorrectly treat the current WebUI Run as Telegram-originated.

### Run and queue behavior

- One Session can have one active Run and queued follow-up work. Queue items carry an executor closure that captures the message and current optional speech input origin, but there is no typed reply-destination field.
- Channel ingress has an additional per-conversation FIFO before work reaches the shared Run queue. It carries the Channel conversation facts and resolves the reply plan when the queued work is processed.
- If WebUI and a Channel both submit work to the same busy Session, any future reply-destination state must follow actual Run execution order. Updating a Session-wide destination merely when an ingress message arrives can make the Agent see the wrong destination for queued work.
- Automation, Cron, subagents, continuation Runs, reflections, and internal Channel Runs also use the shared chat/Run machinery. They do not all have a direct human reply destination.

## Already Implemented

Commit `b629a4e8` (`fix(channels): require channel_send for file delivery`) changed the current Agent guidance so that:

- normal Channel text-only replies are described as automatic;
- every Channel file delivery is described as requiring `channel_send` with `file_paths`;
- the Image Tool distinguishes WebUI/Desktop Markdown from Channel file delivery;
- the one-time Channel Session reminder includes the automatic-text and `channel_send` file rules;
- prompt, Tool-description, Image Tool-result, and Channel-reminder tests cover that wording.

The full backend quality gate passed at that commit with 6,761 tests.

## Known Limitation in the Implemented Change

The one-time Channel Session reminder now states a permanent delivery rule even though a Channel-bound Session may later be continued through WebUI. The rule is correct for a Telegram-originated Run but can be wrong for a WebUI-originated Run in the same Session. This is not resolved by the commit and must not be treated as the final design.

The Image Tool currently says `WebUI/Desktop`. The user has decided that this should be reduced to `WebUI` because Desktop and browser WebUI have the same relevant behavior for this feature. That follow-up text change has not yet been implemented.

## Confirmed Decisions

1. Every file delivered through a Channel uses `channel_send` with `file_paths`, including a file sent while replying to a Channel-originated message.
2. Normal Channel text-only replies continue through the existing automatic Channel relay.
3. Tool artifacts must not be automatically inferred and sent through a Channel as part of this work. The Agent explicitly uses `channel_send` for Channel files.
4. Desktop and browser WebUI are treated as `WebUI` for the reply-rendering and image-delivery behavior currently in scope.
5. The Agent should not receive the same reply-destination reminder on every Run.
6. The system should establish an initial reply destination and then add a notice only when the destination changes.
7. Change detection compares the incoming work with the last active reply destination, not with the Session's original destination. Example: Telegram → Telegram produces no notice; Telegram → WebUI produces one; additional WebUI → WebUI produces none; WebUI → Telegram produces one.
8. The existing Channel-specific mechanism should become generic enough to cover WebUI and current/future Channels instead of adding one unrelated special case per accessor.
9. `Channel` remains the specialized external messaging-platform accessor; it does not become an umbrella term or product domain for WebUI, Desktop, or every other accessor. The shared Run-related abstraction for the automatic reply path sits above WebUI and Channels, with `Reply Route` as the current Working Term.
10. No `Reply Route`, `Reply Destination`, or `Reply Context` Glossary entry is approved yet. Those are Working Terms only until the shared abstraction's exact boundary is decided.

## Rejected Directions

1. **A reminder on every Run.** Rejected as repetitive and unnecessary when the destination has not changed.
2. **Comparing every message with the Session's original accessor.** Rejected because a Session that moved from Telegram to WebUI would then receive a redundant WebUI notice on every subsequent WebUI message.
3. **Using the permanent Channel Session note as proof of the current Run's origin.** Rejected because a Channel-bound Session can be continued from WebUI.
4. **Making the Agent distinguish Desktop from browser WebUI for this behavior.** Rejected for now because both use the same relevant renderer and file behavior. This does not establish that Desktop and WebUI are universally interchangeable for unrelated capabilities such as wakeword support.
5. **Using image Markdown in Telegram or another Channel.** Rejected because Channel text relay does not upload the image file and the artifact URL is server-local.
6. **Automatically sending generated artifacts through Channels.** Rejected in favor of explicit `channel_send` file delivery.
7. **Making `Channel` the umbrella term or product domain for WebUI, Desktop, and messaging platforms.** Rejected because the existing Channel concept remains the specialized external messaging-platform accessor; the shared automatic-reply abstraction sits above WebUI and Channels instead.

## Conceptual Direction, Not Yet a Design

The generic automatic-reply abstraction sits above WebUI and Channels. It does not turn WebUI or Desktop into Channels and does not expand the existing Channel product domain; `Reply Route` is the current Working Term for this shared Run-related layer, not an approved Glossary term.

The current working model has two different kinds of state:

1. **Stable Session binding:** A Session may be linked to Telegram or another Channel so `channel_send` has a known target and the Session remains recognizable in WebUI. This binding does not determine the current Run's automatic reply destination.
2. **Last active reply destination:** The destination used by the most recently processed interactive turn. New work compares its own destination with this value. The Agent receives a short notice only on initialization or change.

Illustrative sequence:

| Incoming work | Previous active destination | Agent notice |
|---|---|---|
| First Telegram message | None | Initial Telegram notice |
| Next Telegram message | Telegram | None |
| WebUI message in the same Session | Telegram | Changed-to-WebUI notice |
| Next WebUI message | WebUI | None |
| Telegram message | WebUI | Changed-to-Telegram notice |

Draft wording discussed but not approved as final copy:

```text
Reply destination changed to WebUI. Your reply to this message will be shown in WebUI.
```

```text
Reply destination changed to Telegram. Your reply to this message will be sent to Telegram. Send files with `channel_send` using `file_paths`.
```

The initial Session wording, the change wording, and whether capabilities belong in every change notice remain open.

## Difference From the Old System

| Concern | Current system | Conceptual direction |
|---|---|---|
| Session identity | One persisted Channel note exists only for Channel-created or manually linked Sessions. Ordinary WebUI Sessions have no equivalent notice. | Every interactive Session establishes an initial reply destination, including WebUI. |
| Meaning of Channel metadata | Often implicitly treated as current origin. | Stable binding only; it does not determine the current Run's reply route. |
| Accessor changes | No change detection. A WebUI turn in a Telegram-bound Session still inherits the Telegram note. | Compare each processed interactive turn with the last active destination and notify only on change. |
| Desktop | Frontend knows it is Desktop, Agent does not. | Normalize to WebUI for the capability currently in scope. |
| Queue behavior | Queue items contain execution closures but no reply-destination contract. | Each accepted work item must retain enough destination data to apply changes in execution order. |
| File guidance | Global/Tool text now says Channel files require `channel_send`, but the Agent may not know whether the current Run is a Channel reply. | Current destination notice disambiguates whether the final reply is WebUI-rendered or Channel-delivered. |

## Expected Costs, Risks, and Negative Consequences

1. **More persisted context or more request-time plumbing.** A reliable destination signal must travel with work and reach the model. Persisted notes add history and tokens; request-only state risks disappearing across tool cycles, reloads, or compaction unless deliberately preserved.
2. **Queue races.** WebUI and Channel work can arrive while a Session is busy. A single Session metadata value updated on ingress can describe a later queued item rather than the Run currently executing.
3. **Routing/hint divergence.** If one component decides where the reply is actually delivered and another independently builds the Agent hint, they can disagree. The Agent may correctly follow a false hint and still misdeliver a file.
4. **Compaction loss.** If the Agent learns the destination only from an old switch note, compaction may summarize or discard that note. The active destination must remain reconstructable without sending a redundant reminder on every ordinary Run.
5. **Ambiguous destination identity.** Treating all Telegram work as one destination may be insufficient when the Channel id, chat id, or forum topic changes. Treating every target detail as a different destination may create excessive notices.
6. **Background work has no obvious human destination.** Automation, reflection, subagent, continuation, and other internal Runs cannot automatically inherit WebUI or Telegram semantics without a product decision.
7. **Proactive sends can corrupt the state if modeled incorrectly.** Calling `channel_send` from WebUI should not silently make Telegram the active destination for the next normal WebUI reply.
8. **Duplicate Channel output remains possible.** A Channel file may be sent with a Tool caption and then followed by the automatically relayed final assistant text. The desired caption/confirmation behavior is not yet settled.
9. **Client-provided accessor data may be stale or dishonest.** vBot is local-first and unauthenticated, but the authoritative source and validation boundary still need to be explicit.
10. **WebUI/Desktop normalization can age badly.** It is correct for current Markdown/file behavior, but future Desktop-only reply capabilities could require a more precise capability model.
11. **Existing Sessions lack the new state.** The project forbids application-level legacy compatibility and auto-migrations. The design needs a clean current-schema behavior for the first new turn in an existing Session without building a permanent compatibility branch.
12. **Prompt-cache impact.** Putting a changing destination in the System Prompt would change the prompt prefix between Runs. A message-level mechanism is likely safer, but that choice is not yet approved.

## Open Product Decisions

1. **What exactly is a reply destination?** Is it only `WebUI` versus `Channel`, or must a Channel destination include platform, Channel id, chat id, and thread/topic?
2. **What initializes an empty Session?** Session creation, the first visible user message, or the first Run with a human reply route?
3. **What happens for a WebUI-created Session later linked to Telegram before any Telegram message arrives?** Linking is a stable binding operation, but it may or may not count as an active-destination change.
4. **What happens for proactive `channel_send` from WebUI?** The current recommendation is that it does not change the active reply destination, but the user has not confirmed this edge case.
5. **Which capabilities belong in the notice?** Destination name only, or the short actionable rule for that destination (Markdown for WebUI; automatic text plus `channel_send` files for Telegram/Discord)?
6. **What are the destinations for CLI, Automation, Cron, reflection, subagents, internal Channel Runs, and continuation Runs?** Some have no direct human reply route, and some may inherit a parent route.
7. **Does switching between two Telegram chats or topics count as a destination change for the Agent?** This matters for file targeting and reply style.
8. **Should the WebUI visibly show the current active destination or source switch to the user?** No UI behavior has been discussed.
9. **What should happen to the permanent Channel reminder?** The conceptual direction is to reduce it to stable binding information, but exact wording and compatibility with existing notes remain undecided.
10. **What should the final Image Tool wording be after the generic mechanism exists?** The user decided to replace `WebUI/Desktop` with `WebUI`; the remaining Channel line may still need to reference the new destination hint rather than asking the Agent to infer the current surface.

## Open Technical Decisions

1. **Canonical data model and owner.** Define one typed representation for the current work item's reply destination. Do not spread unstructured strings across WebUI, Channel adapters, Run code, and prompt builders.
2. **Propagation boundary.** Decide how WebUI submits its destination, how the Channel engine supplies its authoritative reply plan, and how the shared chat loop receives both without depending on accessor modules.
3. **Queue ordering.** Destination data must be captured per queued work item and applied when that item actually begins, not globally at arrival time.
4. **Actual delivery authority.** Decide whether the new representation is model guidance only or also becomes the source of truth for final reply routing. A second independent routing state is dangerous.
5. **Persistence.** Decide which parts belong in Session metadata, persisted notes, per-message metadata, Run fields, or request-only context.
6. **Model-context placement.** A switch notice must appear before the relevant visible user message without breaking assistant/tool adjacency or internal-note embedding rules.
7. **Compaction and history rebuild.** The Agent must retain the active destination after compaction and provider/model switches without receiving every-run noise.
8. **Session lifecycle.** Define behavior for `/new`, deletion/recreation of a Channel pointer target, Session fork, `/handoff`, Agent Takeover restrictions, and manually linked Sessions.
9. **Channel migration and topics.** Telegram chat-id migration and forum topic changes must not leave the destination hint pointing at a stale target.
10. **Interaction with `input_origin`.** Speech transcription remains an independent dimension and may coexist with any reply destination.
11. **Tests.** At minimum, cover initial WebUI, initial Telegram, same-source repetition, both switch directions, mixed-source queued work, proactive send without destination change, manually linked Session, `/new`, compaction, existing Session without destination state, and Channel target/topic changes.

## Candidate Invariants to Evaluate

These are recommendations, not confirmed decisions:

- The same authoritative object should drive both the Agent-facing destination notice and actual automatic reply routing wherever possible.
- Stable Channel binding must never be interpreted as proof of the current Run's reply destination.
- Each queued interactive work item must retain its own reply destination until execution.
- A proactive Tool send must not change the automatic reply destination of the current or next ordinary turn.
- Desktop may normalize to WebUI only for explicitly shared capabilities; the data model should not make future Desktop-specific behavior impossible.
- No changing reply-destination text should be placed in the cache-sensitive System Prompt without a deliberate prompt-cache decision.

## Source Evidence

- `webui/src/components/ChatView.svelte`: WebUI `chat.stream` payload has no accessor or connection id.
- `webui/src/lib/clientIdentity.js`, `webui/src/lib/api.js`, and `webui/src/lib/desktopBridge.js`: browser/Desktop identity is detected and sent on the WebSocket presence connection.
- `server/rpc/chat_methods.py` and `server/rpc/validation.py`: chat RPC accepts optional `input_origin`, currently limited to speech transcription, but no reply destination.
- `core/chat/messages.py`: `input_origin` produces only the speech-transcription reminder.
- `core/channels/adapter.py` and `core/channels/engine.py`: one-time Channel reminder, stable Channel metadata, inbound reply plan, and text-only final relay.
- `server/rpc/agent_methods.py`: manual `session.link_channel` writes stable binding metadata and the same persisted Channel reminder.
- `webui/src/lib/sessionListView.js` and `webui/src/components/SessionListDrawer.svelte`: WebUI recognizes and displays Channel-bound Sessions.
- `core/runs/runs.py`, `core/chat/chat.py`, and `core/automation/automation.py`: shared Run queue/executor flow currently carries no typed reply destination and is used by multiple non-accessor producers.
- `.vorch/domain-maps/channels.md`, `.vorch/domain-maps/chat.md`, `.vorch/domain-maps/runs.md`, `.vorch/domain-maps/webui.md`, `.vorch/domain-maps/desktop.md`, and `.vorch/domain-maps/server.md`: current domain contracts and accessor boundaries.

## Recommended Next Discussion Order

This is a discussion order, not an implementation plan:

1. Define the reply-destination identity and granularity, including Channel/chat/topic boundaries.
2. Classify every Run producer as having a direct destination, inheriting one, or having none.
3. Decide whether the destination object is guidance-only or the authority for actual delivery.
4. Decide persistence, switch detection, queue ordering, and compaction behavior together; they are coupled and should not be approved separately.
5. Approve exact Agent-facing initial and change wording with before/after text.
6. Only then produce an implementation plan and change code.
