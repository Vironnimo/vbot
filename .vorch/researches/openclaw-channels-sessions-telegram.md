## Research: OpenClaw channels, sessions, and Telegram

### Question
How does the open source project OpenClaw model multi-channel messaging, sessions, agent routing, and Telegram integration?

### TL;DR
The correct repository is https://github.com/openclaw/openclaw. OpenClaw uses a gateway-centric architecture where messaging platforms are implemented as channel plugins, inbound traffic is normalized into a shared turn kernel, and sessions are keyed by deterministic route/session keys rather than by UI client state.

The design is notably strong in three areas: a clean separation between channel-owned transport concerns and core-owned session/routing concerns, deterministic agent selection via bindings, and explicit cross-channel continuity primitives (`session.identityLinks` and channel docking) instead of implicit conversation merging.

### Findings

#### Repository
- GitHub repository: https://github.com/openclaw/openclaw
- Project positioning from the README/docs: a personal AI assistant with a single Gateway that owns sessions, channels, tools, and events.

#### 1. Channel abstraction
OpenClaw models Telegram, Discord, Slack, and similar platforms as channel plugins.

Key documented boundaries:
- Core owns the shared `message` tool, prompt wiring, outer session-key shape, generic thread bookkeeping, and dispatch.
- The channel plugin owns:
  - config and account resolution
  - DM/group security and allowlists
  - pairing flows
  - session grammar for provider-specific conversation ids
  - outbound transport to the native platform
  - threading behavior
  - optional typing/heartbeat behavior

The plugin contract is fact-oriented, not transport-specific. The shared inbound turn kernel separates:
- `ConversationFacts`: where the message came from
- `RouteFacts`: which agent/session should handle it
- `ReplyPlanFacts`: where visible replies should go
- `MessageFacts`: what the agent should see

This is a strong abstraction because it avoids collapsing platform conversation ids, session ids, and reply targets into one overloaded identifier.

#### 2. Session management per channel
OpenClaw uses deterministic session keys, not per-client ephemeral sessions.

Documented defaults:
- Direct messages: shared session by default
- Group chats: isolated per group
- Rooms/channels: isolated per room/channel
- Cron jobs: fresh session per run
- Webhooks: isolated per hook

Representative session-key shapes:
- Main/direct chat: `agent:<agentId>:<mainKey>`
- Group: `agent:<agentId>:<channel>:group:<id>`
- Channel/room: `agent:<agentId>:<channel>:channel:<id>`
- Discord/Slack thread: append `:thread:<threadId>`
- Telegram forum topic: append `:topic:<topicId>`

DM isolation is configurable:
- `main` (default): all DMs share one session
- `per-peer`
- `per-channel-peer`
- `per-account-channel-peer`

So yes, there is a per-conversation session strategy, but it is configurable and explicit.

#### 3. One agent, multiple channels
Yes. Multiple channels, accounts, and peers can point to one agent.

OpenClaw uses:
- `agents.list` for agent definitions
- `bindings` to route inbound channel/account/peer traffic to an `agentId`

Routing precedence is deterministic and documented:
1. exact peer match
2. parent peer match
3. Discord guild + roles
4. Discord guild
5. Slack team
6. account match
7. channel match
8. default agent

This means one agent can be the default brain for several channels or channel accounts. The docs explicitly show examples such as routing both Telegram and other channels to the same agent, or multiple channel accounts to different agents.

OpenClaw also supports the inverse pattern:
- multiple agents on the same peer via broadcast groups
- per-topic Telegram agent routing inside one supergroup

#### 4. Inbound routing: platform -> agent
Inbound message flow is standardized through the shared channel turn kernel.

The documented pipeline is:
1. ingest raw platform event
2. classify event kind
3. preflight checks such as dedupe/self-echo/hydration
4. resolve route, reply plan, and message facts
5. authorize DM/group/mention/command access
6. assemble final message context
7. record inbound session metadata and last route
8. dispatch the agent turn
9. finalize channel-local cleanup/state

Important details:
- Channel plugins keep platform-specific auth, transport, native commands, media, and presence behavior.
- Core applies generic policy: DM/group allowlists, pairing-store DM entries, route gates, command gates, mention activation, redacted diagnostics, and admission.
- The kernel returns structured admissions such as `dispatch`, `observeOnly`, `handled`, or `drop`.

This is effectively a normalized ingress pipeline with channel-local evidence gathering and core-owned policy decisions.

#### 5. Outbound sending: agent -> platform
Outbound sending is also split cleanly.

Core owns:
- shared `message` tool
- durable delivery lifecycle
- queueing/write-ahead intent
- generic retry/recovery
- message-sending hooks
- receipts

Channel plugins own:
- native send/edit/delete/reaction/platform API calls
- target normalization
- platform threading/reply semantics
- platform-specific side effects

The plugin exposes a `message` adapter describing its durable final-send capabilities, for example whether it truly supports:
- text
- media
- reply-to
- thread/topic delivery
- rich payloads
- silent delivery
- reconciliation of uncertain sends

This is one of the strongest parts of the design: outbound sends go through one core lifecycle, but only when the plugin proves it can preserve semantics.

#### 6. Cross-channel session continuity
OpenClaw supports this, but it is explicit rather than magical.

There are three relevant mechanisms:
- Shared main DM session by default: in a single-user setup, DMs already collapse into the agent main session.
- `session.identityLinks`: lets the same person across channels share one session identity when using isolated DM scope.
- Channel docking: moves the reply route for the current session to another linked channel without creating a new session.

Example from the docs:
- Telegram sender and Discord sender can be linked under one identity group.
- A `/dock_discord` command keeps the same session/transcript and changes future replies to Discord.

The docs are explicit that docking:
- does change `lastChannel`, `lastTo`, and `lastAccountId`
- does not create a new session
- does not move transcript history to another session
- does not bypass access control

For Web UI specifically:
- WebChat attaches to the selected agent and defaults to that agent's main session.
- The docs state this is how you can see cross-channel context for that agent in one place.

So the answer is: yes, but only for flows that share or deliberately link a session. Group/channel sessions remain isolated by default.

#### 7. Session storage
Session state is gateway-owned and disk-backed.

Two persistence layers are documented:
1. `sessions.json`
   - key/value map from `sessionKey` to `SessionEntry`
   - stores mutable session metadata
2. `<sessionId>.jsonl`
   - append-only transcript
   - stores actual conversation history, tool calls, compaction summaries, and lineage

Default locations:
- `~/.openclaw/agents/<agentId>/sessions/sessions.json`
- `~/.openclaw/agents/<agentId>/sessions/<sessionId>.jsonl`

Important store fields include:
- `sessionId`
- `sessionStartedAt`
- `lastInteractionAt`
- `updatedAt`
- optional `sessionFile`
- `chatType`
- route/display metadata such as provider/subject/room/space/displayName
- model/toggle overrides
- token counters
- compaction/memory-flush bookkeeping

Telegram-specific note from the deep-dive docs:
- Telegram topic sessions may use topic-qualified transcript naming.

This is a pragmatic storage design: a small mutable index plus append-only transcripts.

#### 8. Telegram listener implementation
Telegram is documented as:
- implemented via grammY
- long polling by default
- webhook mode optional

Long polling details:
- uses grammY runner
- per-chat/per-thread sequencing
- guarded so only one poller per bot token is active in a gateway process
- watchdog restarts after 120s without completed `getUpdates` liveness by default
- a 45-second `getUpdates` request guard is used

Webhook details:
- enable with `channels.telegram.webhookUrl` and `channels.telegram.webhookSecret`
- optional `webhookPath`, `webhookHost`, `webhookPort`
- default local listener: `127.0.0.1:8787` and `/telegram-webhook`
- validates request guards, secret token, and JSON body before acknowledging Telegram
- processes updates asynchronously through the same per-chat/per-topic lanes used by polling

So the listener model is clearly dual-mode, with long polling as the operational default.

### Particularly interesting design decisions worth borrowing
- Separate conversation origin, session routing key, and visible reply target. This is the cleanest answer in the repo to cross-channel and threaded messaging complexity.
- Use deterministic route/session keys instead of frontend session ids. That makes channel, agent, and storage behavior predictable.
- Keep platform plugins responsible for transport and native affordances, while core owns policy, routing, and session recording.
- Make cross-channel continuity explicit with `identityLinks` and docking instead of silently merging chats.
- Use a small mutable session index plus append-only JSONL transcripts. This gives cheap listing/mutation and durable replayable history.
- Treat outbound delivery as a capability-declared lifecycle with receipts and durable intent, rather than letting every plugin reinvent send/retry/recovery semantics.
- Model inbound authorization as a shared ingress resolver fed by channel-local facts. That is a strong way to centralize allowlist/pairing/mention logic without overfitting to one platform.

### Caveats
- The default DM behavior is intentionally single-user friendly: all DMs share one session unless `session.dmScope` is tightened. That is convenient, but unsafe for true multi-user bots.
- Cross-channel continuity is not universal. It exists for linked identities and docked sessions, but group/channel sessions remain intentionally isolated.
- The repo is evolving quickly, so some surfaces are explicitly marked as migration/compatibility APIs.

### Sources
- https://github.com/openclaw/openclaw — repository
- https://docs.openclaw.ai/channels/channel-routing — routing rules and session-key shapes
- https://docs.openclaw.ai/concepts/session — session lifecycle and storage
- https://docs.openclaw.ai/concepts/multi-agent — bindings, multi-account, one agent vs many agents
- https://docs.openclaw.ai/concepts/channel-docking — cross-channel continuity
- https://docs.openclaw.ai/channels/telegram — Telegram transport/runtime details
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/sdk-channel-plugins.md — channel abstraction
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/sdk-channel-turn.md — inbound turn kernel
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/sdk-channel-message.md — outbound delivery lifecycle
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/sdk-channel-ingress.md — inbound authorization model
