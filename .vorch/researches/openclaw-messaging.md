## Research: OpenClaw multi-channel messaging, sessions, and Telegram

### Question
How does the open source project OpenClaw handle multi-channel messaging, sessions, one-agent-many-channels routing, outbound delivery, cross-channel continuity, session storage, and Telegram transport details?

### TL;DR
The correct repository is https://github.com/openclaw/openclaw. OpenClaw models channels as plugins over a shared inbound turn kernel and a shared outbound/message layer, while keeping platform-specific security, pairing, target grammar, threading, and transport logic inside each channel plugin.

Sessions are owned by the gateway and stored per agent, with session keys derived from channel, account, peer, and thread/topic depending on `session.dmScope`. Cross-channel continuity is explicit rather than magical: OpenClaw uses `session.identityLinks` to collapse direct-message identities across channels and `/dock_*` commands to move the reply route of an existing session without recreating it.

### Findings

#### Correct repository
- Repository: https://github.com/openclaw/openclaw
- README describes it as a personal AI assistant that answers on existing channels, including Telegram, Discord, Slack, WhatsApp, Signal, iMessage, Matrix, and others.
- The repo includes both high-level docs and concrete implementation for Telegram under `extensions/telegram/`.

#### 1. How channels are modeled as an abstraction
- Channels are plugin-based. The channel plugin contract gives each channel responsibility for:
  - config and account resolution
  - DM/group security and allowlists
  - pairing flows
  - session grammar and target parsing
  - outbound transport
  - reply threading and optional typing/heartbeat behavior
- Core deliberately keeps one shared `message` tool and one shared inbound turn kernel.
- The inbound turn kernel uses a fact model that separates:
  - `ConversationFacts`: where the message came from
  - `RouteFacts`: which agent/session should process it
  - `ReplyPlanFacts`: where replies should go
  - `MessageFacts`: what text/context the agent should see
- This is a strong design choice: OpenClaw explicitly treats conversation identity, routing identity, reply destination, and model-visible content as different layers.
- The kernel stage pipeline is fixed across channels:
  1. `ingest`
  2. `classify`
  3. `preflight`
  4. `resolve`
  5. `authorize`
  6. `assemble`
  7. `record`
  8. `dispatch`
  9. `finalize`
- Shared runtime wiring lives in `src/plugins/runtime/runtime-channel.ts`, which exposes routing, session recording, pairing, mention gating, outbound adapter loading, and turn execution to channel plugins.

#### 2. How sessions are managed per channel / per conversation
- Sessions are gateway-owned and agent-scoped.
- Each agent has its own session store under `~/.openclaw/agents/<agentId>/sessions`.
- Routing rules by conversation type are documented clearly:
  - direct messages: shared session by default
  - group chats: isolated per group
  - rooms/channels: isolated per room
  - cron jobs: fresh session per run
  - webhooks: isolated per hook
- Session keys are shaped like:
  - direct/main: `agent:<agentId>:main`
  - group: `agent:<agentId>:<channel>:group:<id>`
  - room/channel: `agent:<agentId>:<channel>:channel:<id>`
  - thread variants append thread or topic suffixes
- Telegram forum topics are isolated with `:topic:<threadId>`.
- DM scoping is configurable with `session.dmScope`:
  - `main` (default): all DMs share one session
  - `per-peer`: isolate by sender, potentially across channels
  - `per-channel-peer`: isolate by channel + sender
  - `per-account-channel-peer`: isolate by account + channel + sender
- `resolve-route.ts` feeds `dmScope` and `identityLinks` into `buildAgentSessionKey(...)`, so DM scoping is not a Telegram-specific hack; it is part of the generic routing/session model.

#### 3. One agent, multiple channels
- OpenClaw’s core abstraction is “agent = brain” and “binding = route inbound traffic to that brain”.
- An agent has its own:
  - workspace
  - agentDir / auth profiles
  - session store
- Inbound traffic is mapped to agents through `bindings[]` rules.
- The docs and resolver implementation show no one-binding-per-agent restriction. Multiple bindings can target the same `agentId`, so yes: multiple bots/channel entries can point to one agent.
- This works both across channels and within a channel’s multiple accounts.
- Examples in the docs show channel/account-specific bindings, such as distinct Telegram bot accounts routing to different agents. The model also naturally supports the inverse: many bindings targeting the same agent.
- OpenClaw also has a separate “broadcast groups” mechanism that can intentionally run multiple agents for the same inbound peer when the platform event is otherwise reply-eligible.
- Telegram adds another routing dimension for forum topics: per-topic `agentId` overrides allow different topics in the same supergroup to route to different agents.

#### 4. Inbound message routing: platform -> agent
- Generic path:
  1. Channel transport receives a platform event.
  2. Plugin adapter normalizes it into `NormalizedTurnInput` plus fact bundles.
  3. `resolveAgentRoute(...)` chooses an agent and session key.
  4. `buildChannelTurnContext(...)` produces the normalized message context.
  5. `recordInboundSession(...)` persists inbound metadata and last-route state.
  6. The turn kernel dispatches the agent run.
- `resolveAgentRoute(...)` uses deterministic precedence:
  1. exact peer
  2. parent peer (thread inheritance)
  3. guild + roles (Discord)
  4. guild
  5. team (Slack)
  6. account match
  7. channel match
  8. default agent
- `recordInboundSession(...)` persists session metadata first, then optionally updates `lastRoute` for delivery.
- For shared main DM sessions, OpenClaw has a “main DM route pinning” behavior to avoid unrelated inbound DMs overwriting the main session’s reply route.
- Telegram-specific inbound behavior:
  - implemented with grammY
  - inbound messages normalize into the shared envelope with reply metadata, media placeholders, and observed reply-chain context
  - long polling uses per-chat/per-thread sequencing
  - group sessions isolate by group id, and topics isolate by `:topic:<threadId>`
  - in long-poll mode, update offsets are persisted only after successful dispatch, so failed updates remain retryable
- Telegram dispatch itself is channel-owned and rich: `extensions/telegram/src/bot-message-dispatch.ts` builds a prepared turn and uses the shared kernel while keeping Telegram preview/edit/retry/typing behavior local.

#### 5. Outbound sending: agent -> platform
- Outbound starts from the active session context plus optional explicit target overrides.
- `resolveAgentDeliveryPlan(...)` decides:
  - resolved channel
  - resolved `to`
  - resolved account id
  - resolved thread id
  - target mode (`implicit` vs `explicit`)
- Important safety rule: if the current turn originated from a specific channel, `turnSourceChannel` overrides session-level `lastChannel` so shared `dmScope="main"` sessions do not accidentally reply on the wrong channel.
- `resolveAgentOutboundTarget(...)` then normalizes the final platform target when needed.
- Telegram outbound is provided by a channel-specific outbound adapter plus a message adapter:
  - `createTelegramOutboundAdapter(...)`
  - `createChannelMessageAdapterFromOutbound(...)`
- Telegram advertises live delivery capabilities such as:
  - draft preview
  - preview finalization
  - progress updates
  - final-edit / fallback / receipt-aware behavior
- Telegram can:
  - send/edit/delete/react
  - stream preview replies via `sendMessage` + `editMessageText`
  - do durable final sends
  - send typing before certain payloads, including exec approval prompts via `beforeDeliverPayload`
- The actual Telegram dispatcher is intentionally channel-owned because it manages preview lanes, reasoning streaming, inline buttons, quote replies, topic/thread handling, and fallback semantics.

#### 6. Cross-channel session continuity
- OpenClaw supports this explicitly in two ways.
- First, `session.identityLinks` lets you declare that multiple channel identities belong to the same person, for example:
  - `telegram:123`
  - `discord:456`
- When identity links are present, DM session key derivation can collapse those identities into one canonical direct-message thread, depending on `dmScope`.
- Second, channel docking keeps the same session and transcript but changes delivery routing for later replies.
- `/dock_*` commands update session delivery fields like:
  - `lastChannel`
  - `lastTo`
  - `lastAccountId`
- Docking does not recreate the session, move transcript history, or bypass access control. It only changes where future replies for that same session are delivered.
- The docs explicitly describe the use case “start on Telegram, continue replies on Discord” as a first-class workflow.
- WebChat also participates here in a weaker but useful way: it attaches to the selected agent and defaults to the agent’s main session, so it can show cross-channel context for that agent in one place.

#### 7. Session storage
- OpenClaw uses two persistence layers:
  1. `sessions.json`
  2. append-only transcript JSONL files
- Per-agent paths:
  - store: `~/.openclaw/agents/<agentId>/sessions/sessions.json`
  - transcript: `~/.openclaw/agents/<agentId>/sessions/<sessionId>.jsonl`
- Topic transcript files may include a topic suffix such as `-topic-<threadId>.jsonl`.
- `sessions.json` is a mutable key/value map from `sessionKey -> SessionEntry`.
- It stores metadata such as:
  - current `sessionId`
  - `sessionStartedAt`
  - `lastInteractionAt`
  - `updatedAt`
  - `chatType`
  - `lastChannel` / `lastTo` / delivery context
  - send policy and model/auth overrides
  - token counters
  - compaction bookkeeping
- The transcript file is append-only JSONL with a tree structure:
  - first line: session header
  - later lines: messages, tool results, custom entries, compaction summaries, branch summaries
- This split is deliberate: metadata stays cheap and mutable, while full history stays append-only and replayable.

#### 8. Telegram listener: long polling vs webhook
- Telegram uses grammY.
- Default mode is long polling.
- Webhook mode is optional.
- Startup selection happens in `monitorTelegramProvider(...)`:
  - if `useWebhook` / `webhookUrl` is configured, start webhook runtime
  - otherwise start long polling runtime
- Long polling implementation details:
  - uses `@grammyjs/runner`
  - uses a polling lease so only one active poller per bot token runs at a time
  - attempts `deleteWebhook()` before polling
  - uses a restart/backoff loop on recoverable failures
  - detects `getUpdates` 409 conflicts and marks transport dirty before retrying
  - uses a stall watchdog, default 120 seconds, configurable via `pollingStallThresholdMs`
  - can rebuild the Telegram transport after recoverable network issues or stalls
  - can optionally run isolated ingress spooling/worker flow
- Webhook implementation details:
  - local HTTP listener defaults to `127.0.0.1:8787`
  - default path `/telegram-webhook`
  - requires a non-empty webhook secret
  - validates request guards, rate limit, Telegram secret header, and JSON body
  - returns `200` before running the update handler
  - calls `bot.handleUpdate(...)` asynchronously after ACK so slow agent turns do not block Telegram delivery acknowledgements
  - advertises the webhook to Telegram with retry on recoverable failures/server errors/rate limits

### Design decisions worth borrowing
- Separate `ConversationFacts`, `RouteFacts`, `ReplyPlanFacts`, and `MessageFacts`. This prevents routing identity, reply identity, and model-visible content from becoming conflated.
- Keep a shared inbound turn kernel, but leave transports and delivery semantics inside plugins. OpenClaw centralizes the boring/fragile orchestration while preserving channel-specific behavior.
- Make DM scoping explicit with `dmScope`, and add `identityLinks` for safe cross-channel continuity.
- Add an explicit “dock” operation instead of trying to infer reply-channel switching automatically.
- Guard against misdelivery in shared sessions by passing the current turn’s source channel into outbound planning.
- Persist mutable session metadata separately from append-only transcripts.
- In Telegram polling, persist update offsets only after successful dispatch. That is a good at-least-once/retryability tradeoff.
- In webhook mode, ACK fast and process asynchronously through the same sequencing lanes as polling.

### Risks & Caveats
- The default `session.dmScope: "main"` is convenient for one-owner setups but unsafe for shared/multi-user inboxes.
- Docking only changes future delivery routing. It does not create accounts, grant access, or merge unrelated users.
- Telegram DM pairing does not automatically authorize group usage; group access is controlled separately.
- Some delivery details are currently channel-specific because OpenClaw is mid-flight on a broader message-lifecycle migration. The docs are explicit about compatibility shims remaining in place.

### Sources
- https://github.com/openclaw/openclaw
- https://raw.githubusercontent.com/openclaw/openclaw/main/README.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/channels/channel-routing.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/multi-agent.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/session.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/reference/session-management-compaction.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/channel-docking.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/channels/telegram.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/sdk-channel-turn.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/docs/plugins/sdk-channel-plugins.md
- https://raw.githubusercontent.com/openclaw/openclaw/main/src/plugins/runtime/runtime-channel.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/src/routing/resolve-route.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/src/infra/outbound/agent-delivery.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/src/channels/turn/kernel.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/src/channels/turn/context.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/src/channels/session.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/extensions/telegram/src/channel.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/extensions/telegram/src/monitor.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/extensions/telegram/src/polling-session.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/extensions/telegram/src/webhook.ts
- https://raw.githubusercontent.com/openclaw/openclaw/main/extensions/telegram/src/bot-message-dispatch.ts
