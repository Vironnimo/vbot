# Glossary

## Agent
**Definition:** A file-based configuration stored at `<datadir>/agents/<agent-id>/agent.json`. Every agent has a Workspace and stores its chat history in Sessions.
**Not:** A chatbot, a background process, or a chat session. The agent is the configuration — a session is an interaction with it, not the agent itself.

## Agentic Loop
**Definition:** The central processing cycle of a chat. The model receives a user message, responds with text and/or tool calls. If tools are called, they execute and results feed back to the model. This repeats until the model returns a final response with no tool calls. The loop runs entirely in the kernel, with streaming via SSE.
**Not:** An event loop or game loop. Not a separate process — it runs in the same async context.

## Provider
**Definition:** An external API service that hosts AI models (OpenAI, Anthropic, Groq, OpenRouter, local models via Ollama, etc.). A provider consists of two parts: an **Adapter** (code) that speaks the wire protocol, and a **JSON config** (`resources/providers/<name>.json`) that describes base URL, authentication, and provider-specific settings. Each provider also has a **model list** (`resources/models/<provider>.json`) containing all models available through that provider, with their real IDs, capabilities, and metadata. The model ID in the file is the exact ID sent to the API — no remapping.
**Not:** A Model. The provider is the infrastructure that routes the request; the model is the endpoint that processes it.

## Adapter
**Definition:** A code class that speaks a specific wire protocol. The adapter hierarchy:

- `ProviderAdapter` (ABC) — defines the interface: `send()`, `stream()`
- `OpenAICompatibleAdapter` — concrete class for the generic OpenAI `chat/completions` protocol. Used directly by fully compatible providers; mostly compatible providers should subclass it when runtime behavior or catalog discovery differs.
- `OpenCodeGoAdapter`, `OpenRouterAdapter`, and `GitHubCopilotAdapter` — OpenAI-compatible subclasses that own provider-specific runtime or model-catalog knowledge.
- `AnthropicAdapter` — concrete class for Anthropic's Messages API. Own wire protocol, own message format, own thinking/reasoning parameters.
- Custom adapters can inherit from `OpenAICompatibleAdapter` and override specific methods when a provider is mostly but not fully OpenAI-compatible.

**Not:** A model, a provider config, or a data format. The adapter is purely the wire protocol translation layer.

## Connection
**Definition:** One authentication/wire variant of a Provider, declared statically in `resources/providers/<name>.json`: auth type (`api_key`/`oauth`), optional per-connection base URL, wire `mode`, and models endpoint. Addressed as `<provider>:<connection>` (e.g. `openai:api-key`, `openai:subscription`); model catalogs and discovery are connection-scoped.
**Not:** An Account. The Connection defines *how* vBot talks to the provider; the Account decides *which credential* is used on it. Also not a network/HTTP connection.

## Account
**Definition:** A named credential slot on a Connection — one of possibly several API keys or OAuth logins for the same Connection, addressed as `<provider>:<connection>[:<account>]` with default slot `default`. When no account is pinned, the first usable one is chosen deterministically (`default` first, then sorted); API-key accounts map to derived env keys (`BASE__<ACCOUNT>`), OAuth accounts to per-account token files.
**Not:** A Connection, and not a user account in the product sense. Accounts are interchangeable credentials — they never change the wire protocol or the model catalog.

## Model
**Definition:** A specific AI model at a specific provider. Models are always provider-specific — the same underlying model (e.g., Claude Sonnet 4) appears as different entries in different provider model lists, with different IDs, capabilities, and context windows. The model ID is the exact string sent in the API request (e.g., `anthropic/claude-sonnet-4` at OpenRouter, `claude-sonnet-4-20250219` at Anthropic). The user selects a model as `<provider>/<model-id>` (e.g., `openrouter/anthropic/claude-sonnet-4`).

Model data includes: name, capabilities (vision, tools, reasoning, etc.), context window, max output tokens. All of these are provider-specific facts about the model at that provider — not canonical claims that need overrides.
**Not:** A Provider. The model is the cognitive endpoint; the provider is the service that routes the request to it. Not a canonical entity that exists independently of providers.

## Embedding Model
**Definition:** A specialized model that converts text into numerical vectors (embeddings) for semantic comparison. In vBot, this is a configurable `text_embedding` task-model binding used by the recall `vector` backend to find meaning-related past sessions (e.g. "car" and "vehicle" are nearby in embedding space).
**Not:** A chat model, a TTS model, or an image generation model. The embedding model produces vectors, not text, speech, or images.

## Reasoning
**Definition:** A model capability indicating whether the model can perform an internal reasoning step before producing its final answer. In the model data, this is a boolean: `reasoning.supported: true` or `false`. At runtime, the agent's `thinking_effort` field (`none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`, or empty for provider default) controls how much reasoning the model does. Each adapter translates this into its provider's wire format.
**Not:** Chain of Thought. Reasoning is the capability and its configuration; CoT is the opaque output that reasoning produces.

## Chain of Thought (CoT)
**Definition:** The opaque output produced during reasoning — both readable text and provider-specific data (signatures, encrypted content) that must be preserved unchanged for round-tripping. How far persisted CoT replays into later requests is the adapter's per-provider reasoning-replay policy (`none` / `current_run` / `full_history`): within tool-use loops dropping it breaks model continuity, and providers like Anthropic expect it back unchanged across the whole same-model conversation. The adapter handles all serialization and round-trip preservation; the chat layer owns history shaping (which entries keep CoT) but never interprets CoT data.
**Not:** Reasoning. CoT is the opaque output; reasoning is the capability and its configuration.
**Example:** Anthropic returns `thinking` blocks with a `signature` field. Whenever history is replayed — a tool-use loop or, under `full_history`, a later run on the same model — the adapter must send both fields back unchanged; dropping the signature breaks continuity even though vBot never reads it.

## Session
**Definition:** A system-owned chat container under `<datadir>/agents/<agent-id>/sessions/`, persisted as one JSONL file per session. A Session belongs to exactly one Agent and owns the persisted message history. At the product/server level, starting a new Session is an explicit action; once it exists, its file and history are created and maintained by the system.
**Not:** The agent itself, the currently executing work, or the agent's Workspace files. The Session is the persisted conversation container; the Run is the active execution inside it.

## Memory
**Definition:** Curated, durable facts stored in Workspace Markdown files and managed through the memory service/tool. User-scope memory lives in `USER.md`; agent-scope memory lives in `MEMORY.md`. An Agent's `memory_prompt_mode` decides which of those files, if any, become prompt-visible.
**Not:** Session history, scratch notes, or a broad search index. Searchable conversation recall belongs to Sessions and recall tools such as `session_search`.

## Semantic Recall
**Definition:** Meaning-based session search using vector embeddings instead of keyword matching. A session about "vehicles" can match a query for "cars" because their vectors are nearby in embedding space, even though they share no literal words. Enabled by switching `recall.backend` to `vector` and configuring a `text_embedding` model.
**Not:** Keyword search (substring or FTS — that's what `jsonl_scan` and `sqlite_fts` do). Not curated memory or session browsing. Semantic recall retrieves past sessions by meaning, not by exact terms.

## Run
**Definition:** One active execution inside a Session: a user turn plus all model output, visible thinking blocks, tool calls, tool results, and follow-up assistant output until the work completes, fails, or is cancelled.
**Not:** The Agent, the Session, or a single provider HTTP request. A Run can span multiple model/tool steps.

## Accessor
**Definition:** An external interface to the same vBot system, such as the WebUI, Desktop app, CLI, or later other channels. Accessors talk to the vBot server; they do not call providers directly.
**Not:** A Provider or Adapter. An Accessor is a client-facing entry point into vBot.

## Event Bus
**Definition:** The server's internal mechanism for publishing lifecycle events such as run start, streamed output, tool activity, completion, failure, or cancellation to interested clients.
**Not:** A provider protocol, storage format, or public API by itself. It is an internal distribution mechanism.

## Streaming
**Definition:** Incremental delivery of a Run's output while that Run is still executing. In vBot's external server contract, streaming is exposed by the server; provider-specific streaming details stay hidden behind adapters.
**Not:** A separate chat system with different semantics from normal send. It is the same Run, delivered incrementally instead of only at the end.

## Cancel
**Definition:** A best-effort request to stop an active Run as quickly as possible. It stops further model/tool progression, tries to abort the current provider work, and ignores late results that arrive after cancellation.
**Not:** Deleting the Session, rolling back already persisted history, or erasing output that was already shown to the user.

## Skill
**Definition:** A reusable playbook for an agent — a `SKILL.md` file with instructions that teach the agent *how* to handle a specific task or domain. A skill may optionally bundle specialized CLI utilities under a `resources/` subdirectory, but most skills consist solely of the Markdown instructions.
**Not:** A Tool. A tool does one thing; a skill teaches a workflow or convention. The utilities a skill may bundle are specialized CLI programs, not agent-tools.

## System Reminder
**Definition:** A kernel-internal note that is persisted in a Session and later embedded into a provider request as a synthetic user message wrapped in `<system-reminder>` tags. It lets background producers inform the model about events without creating a normal user-visible chat message.
**Not:** A system prompt, a real user turn, or a server/UI notification.

## Tool
**Definition:** A function with a name, a description, and a parameter schema (JSON Schema) that an agent can call during a chat. The agent decides via the agentic loop whether a tool call is needed; the runtime executes it and returns the result to the model. Tools resolve relative paths against the agent's workspace by default.

## Workspace
**Definition:** The agent's home directory at `<datadir>/workspace-<agent-id>/`. Contains Markdown files the agent reads and maintains itself, primarily `SOUL.md`, `USER.md`, and `MEMORY.md`. Tools resolve relative paths against the workspace by default; absolute paths bypass it.
**Not:** The app source directory. Not the sessions directory. The workspace is agent-owned and agent-maintained — sessions are system-owned persisted chat history.
