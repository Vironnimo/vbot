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

## Model
**Definition:** A specific AI model at a specific provider. Models are always provider-specific — the same underlying model (e.g., Claude Sonnet 4) appears as different entries in different provider model lists, with different IDs, capabilities, and context windows. The model ID is the exact string sent in the API request (e.g., `anthropic/claude-sonnet-4` at OpenRouter, `claude-sonnet-4-20250219` at Anthropic). The user selects a model as `<provider>/<model-id>` (e.g., `openrouter/anthropic/claude-sonnet-4`).

Model data includes: name, capabilities (vision, tools, reasoning, etc.), context window, max output tokens. All of these are provider-specific facts about the model at that provider — not canonical claims that need overrides.
**Not:** A Provider. The model is the cognitive endpoint; the provider is the service that routes the request to it. Not a canonical entity that exists independently of providers.

## Reasoning
**Definition:** A model capability indicating whether the model can perform an internal reasoning step before producing its final answer. In the model data, this is a boolean: `reasoning.supported: true` or `false`. At runtime, the agent's `thinking_effort` field (`none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`, or empty for provider default) controls how much reasoning the model does. Each adapter translates this into its provider's wire format.
**Not:** Chain of Thought. Reasoning is the capability and its configuration; CoT is the opaque output that reasoning produces.

## Chain of Thought (CoT)
**Definition:** The opaque output produced during reasoning — both readable text and provider-specific data (signatures, encrypted content) that must be preserved unchanged for round-tripping, especially during tool-use loops where dropping it breaks model continuity. The adapter handles all serialization and round-trip preservation; the chat layer never interprets CoT data.
**Not:** Reasoning. CoT is the opaque output; reasoning is the capability and its configuration.
**Example:** Anthropic returns `thinking` blocks with a `signature` field. During a tool-use loop, the adapter must send both fields back unchanged — dropping the signature breaks continuity even though vBot never reads it.

## Session
**Definition:** A system-owned chat container under `<datadir>/agents/<agent-id>/sessions/`, persisted as one JSONL file per session. A Session belongs to exactly one Agent and owns the persisted message history. At the product/server level, starting a new Session is an explicit action; once it exists, its file and history are created and maintained by the system.
**Not:** The agent itself, the currently executing work, or the agent's Workspace files. The Session is the persisted conversation container; the Run is the active execution inside it.

## Memory
**Definition:** Curated, durable facts stored in Workspace Markdown files and managed through the memory service/tool. User-scope memory lives in `USER.md`; agent-scope memory lives in `MEMORY.md`. An Agent's `memory_prompt_mode` decides which of those files, if any, become prompt-visible.
**Not:** Session history, scratch notes, or a broad search index. Searchable conversation recall belongs to Sessions and recall tools such as `session_search`.

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
