# Glossary

## Agent
**Definition:** A file-based configuration stored at `<datadir>/agents/<agent-id>/agent.json`. Every agent has a Workspace and stores its chat history in Sessions.
**Not:** A chatbot, a background process, or a chat session. The agent is the configuration — a session is an interaction with it, not the agent itself.

## Agentic Loop
**Definition:** The central processing cycle of a chat. The model receives a user message, responds with text and/or tool calls. If tools are called, they execute and results feed back to the model. This repeats until the model returns a final response with no tool calls. The loop runs entirely in the kernel, with streaming via SSE.
**Not:** An event loop or game loop. Not a separate process — it runs in the same async context.

## Provider
**Definition:** An external API service that hosts AI models (OpenAI, Anthropic, Groq, OpenRouter, local models via Ollama, etc.). A provider consists of two parts: an **Adapter** (code) that speaks the wire protocol, and a **JSON config** (`resources/providers/<name>.json`) that describes base URL, authentication, and provider-specific settings. Each provider's models come from the layered Model DB under `resources/models/` — a generated per-provider file plus optional hand overrides, assembled at load over a shared canonical base. The model ID in those files is the exact ID sent to the API — no remapping (the canonical base is reached by an internal join; see Canonical id).
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

Model data — name, typed capabilities (vision, tools, reasoning, …), context window (optional), max output tokens — is assembled at load from up to three layers: a shared canonical base, the provider layer, and hand overrides. The same underlying model stays a distinct entry per provider, now optionally enriched from the canonical base via the deterministic join.
**Not:** A Provider, and not the same thing as its Canonical id. The model is the cognitive endpoint the provider routes to; the wire `model-id` goes on the wire, while the canonical id is an internal join key that never does.

## Canonical id
**Definition:** A models.dev-style `lab/model` identifier (e.g. `deepseek/deepseek-v4-pro`) that names a model independent of any provider. It is a purely internal join/DB key used during at-load assembly to inherit shared base facts onto a provider model — it is **never sent on the wire**. A provider model reaches its canonical base by an explicit `canonical` pointer (hand or auto) or an exact wire-id match; resolution is deterministic only, never fuzzy.
**Not:** A wire `model-id` (the exact string an API expects). The canonical id never leaves assembly; the model-id is what providers receive. A missed join is not an error — the model runs on provider + override data.

## Refresh
**Definition:** The DUMB half of the Model DB: fetch provider `/models` and the public models.dev `catalog.json`, then project the results onto disk per file. Needs network and (for provider catalogs) a credential; rare and explicit (`model.refresh_db`). It writes the pure per-file projection — no merge across files, no join across providers.
**Not:** Load. Refresh writes disk from the network; a hand-edit to an override file takes effect on the next **Load**, with no refresh.

## Load
**Definition:** The SMART half of the Model DB (`ModelRegistry.load` → `assembly.py`): assemble each effective model in memory from the on-disk layers — resolving the canonical join and the field-level merge — with no network and no key. Frequent (startup, after cache invalidation).
**Not:** Refresh, and not generic file loading. Load does the cross-file assembly Refresh deliberately avoids; it reads the layer files but fetches nothing.

## Embedding Model
**Definition:** A specialized model that converts text into numerical vectors (embeddings) for semantic comparison. In vBot, this is a configurable `text_embedding` task-model binding used by the recall `vector` backend to find meaning-related past sessions (e.g. "car" and "vehicle" are nearby in embedding space).
**Not:** A chat model, a TTS model, or an image generation model. The embedding model produces vectors, not text, speech, or images.

## Reasoning
**Definition:** A model capability for an internal reasoning step before the final answer. In the model data it is a typed block, not a bare boolean: `reasoning.supported` (bool) plus, when supported, a `control` (see Reasoning control) and its parameters (a `levels` effort ladder or a `budget_max`). At runtime the agent's `thinking_effort` field (`none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`, or empty for provider default) and the model's `control` are turned into a provider-neutral **reasoning intent** (see Reasoning intent) by one shared decision layer; each adapter only *renders* that intent into its own wire vocabulary. A `levels` model snaps the effort and sends it; an `on_off` model toggles thinking on/off natively; a `budget` model sends a native token budget derived from the effort (Anthropic `budget_tokens`, scaled by `budget_max` when known) — OpenRouter is the deliberate exception, mapping effort→budget itself so vBot sends an effort there too. `/status` reports what actually reaches the wire: the snapped effort, `on`/`off`, or the rendered budget (`on (16,384 tokens)`).
**Not:** Chain of Thought. Reasoning is the capability and its configuration; CoT is the opaque output that reasoning produces.

## Reasoning control
**Definition:** How a provider steers a model's reasoning on the wire — one of `levels` (an effort ladder), `on_off` (a thinking toggle), or `budget` (a token budget, with `budget_max`). vBot derives it at refresh from the models.dev `reasoning_options` source field (an `effort` option wins → `levels`; else `budget_tokens` → `budget`; else `toggle` → `on_off`) and stores it in `capabilities.reasoning.control`.
**Not:** The `thinking_effort` the agent selects. `control` is the model's wire capability; `thinking_effort` is the per-agent setting that gets snapped against the model's `levels` ladder.

## Reasoning intent
**Definition:** The provider-neutral description of what a single request should ask of the model's reasoning, produced once by `resolve_reasoning_intent(...)` (in `core/providers/reasoning.py`) from `(model control, agent effort)`. One of five kinds: `default` (no effort selected — leave the provider default untouched), `off` (do not reason), `effort` (reason at a snapped effort level), `budget` (reason within a token budget), or `on` (reason, binary toggle). Each adapter has a small *render* step that translates the intent into its own wire fields — adding a future provider or control kind is a new render, never new policy. The effort→budget math, `none`→off mapping, and `max_tokens` clamp all live in this one resolver.
**Not:** A wire payload or a provider parameter. The intent is the shared vocabulary *between* the decision and the per-adapter render; it never goes on the wire as-is.

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
**Definition:** A function with a name, a description, and a parameter schema (JSON Schema) that an agent can call during a chat. The agent decides via the agentic loop whether a tool call is needed; the runtime executes it and returns the result to the model. File tools resolve relative paths against the **cwd** by default (the project repo for a project Session, else the agent's Workspace); the `memory` tool stays on the Workspace.

## Workspace
**Definition:** The agent's identity/memory home directory at `<datadir>/workspace-<agent-id>/`. Contains Markdown files the agent reads and maintains itself, primarily `SOUL.md`, `USER.md`, and `MEMORY.md`. The Workspace is **no longer** where file tools resolve relative paths — that is now the **cwd**, a separate runtime field (the project repo for a project Session, the Workspace otherwise). The Workspace stays the home of the `memory` tool; absolute paths bypass both.
**Not:** The app source directory. Not the sessions directory. Not the cwd — the cwd is where file tools work, the Workspace is the identity/memory home; they coincide only for an identity agent at home. The workspace is agent-owned and agent-maintained — sessions are system-owned persisted chat history.

## Project
**Definition:** A first-class entity (not just a cwd), keyed by a stable `project_id` slug with a changeable display name, that bundles a cwd (the repo directory tools resolve relative paths against), an auto-load file list (`AGENTS.md` is seeded as its first entry at creation, then a normal removable entry), a project-default-agent, a default-model, a team scanned live from the repo, and Sessions. The minimal Project is just a cwd — team and auto-load files are all optional, so an empty folder is a valid Project.
**Not:** A bare cwd, a Workspace, or an Agent. The cwd is one field of a Project; a Workspace is an Agent's identity home in the data-dir, never the project repo. vBot reads the repo (to discover the Team) but never writes it — runtime data (the project anchor, Sessions) lives in the data-dir.

## Project Anchor
**Definition:** A project's runtime home in the **data-dir**: `<datadir>/projects/<project-id>/`, holding `project.json` plus a thin per-agent anchor (`agents/<agent-id>/` with `sessions/` and, only for a rooted identity agent, `workspace/`). The anchor holds **no run config** — only Sessions ownership and the local agent id; an agent's config comes live from the repo scan. The key is the stable `project_id` slug; the cwd path lives in `project.json`, so the repo folder can move without breaking the anchor or its Sessions.
**Not:** The repo (cwd). The anchor is what vBot *creates* in the data-dir; the repo is what the user/team *writes* and vBot never touches. Removing a project archives the anchor, never the repo.

## Team
**Definition:** The set of agents discovered in a Project by the **scan** of the repo (each format's known location, non-recursive — OpenCode reads only `.opencode/agents/`). The team is the project's roster of callable agents; it is re-derived from the repo on open / explicit re-scan (the repo is the source of truth, no copy drift). A bare/empty project has an empty team — that is normal, not an error.
**Not:** The global Agent store. Team membership is project-scoped and lives in the repo, not in the data-dir agent store. A visiting identity agent is **not** a team member.

## Config Agent
**Definition:** An Agent that is *only* a profile — model, tools, prompt body, temperature, thinking effort — with **no Workspace and no identity** (and, in v1, **no memory tool**). Its temperature and thinking effort resolve through the same Agent → Project default → global → Provider-default chain as its model. The typical Project Agent: a scanned OpenCode agent. At runtime it is a `ConfigAgent` synthesized from the scan (`workspace=""`, `memory_prompt_mode="off"`, plus a verbatim prompt `body`); its `allowed_tools`/`allowed_skills` are computed from the project's whitelists (the Project Tool Whitelist minus the agent's OpenCode denials, and the project-derived skills), not the old `["*"]`. If it wants durable notes it writes a normal file in its cwd (the repo), via the file tools — agent work, not vBot runtime state.
**Not:** An Identity Agent. A Config Agent has no SOUL/USER/MEMORY home and no memory tool; it is interchangeable run-config, not a persistent identity.

## Identity Agent
**Definition:** An Agent with a Workspace (`SOUL.md`/`USER.md`/`MEMORY.md`) and a memory tool — the existing store-backed agent under `<datadir>/agents/<id>/`. It carries durable identity/memory across sessions and brings its own model (model → global default). Both Config and Identity agents resolve through the same `resolve_agent` seam into the uniform `RuntimeAgent`.
**Not:** A Config Agent. The Identity Agent is the persistent self with a memory home; a Config Agent is a bare profile.

## Project Agent
**Definition:** A member of a Project's Team — an agent discovered by the scan of that project's repo. In v1 a Project Agent is always a Config Agent (OpenCode); identity-bearing formats will later contribute Identity Agents as team members. Inside a project an agent is called by its bare id (against *that* project's team); from outside it is addressed project-qualified as `agent@projekt` (e.g. `orchestrator@vbot`), so a bare `builder` stays unambiguous across projects.
**Not:** A visiting identity agent. A Project Agent belongs to (is born in) the project; a visitor only reaches in.

## Visiting (Visit)
**Definition:** An identity agent (e.g. your main agent) reaching into a Project it is **not** homed in — told "work on the project at <path>". Its **cwd stays its own home** (unchanged), and the project files arrive as a `<system-reminder>` in context rather than in the system prompt; it uses the path absolutely. The visited project lives only in the session meta + reminder, never in the session path. **The visit triggers itself** — no command or button: when the agent's file tools reach into a registered Project's repo by absolute path, that Project's auto-load files (with `AGENTS.md` seeded first) are injected once per project per session (recorded in the session meta).
**Not:** A Project Agent, nor a rooted identity agent (whose home *is* the project, cwd = repo, project files in the system prompt). A visit is a reach-in from a home elsewhere.

## Ceiling
**Definition:** The hard maximum a Project sets for its Team's Config Agents — what an agent *may* use, never a guarantee of what it *does* use. The Project Tool Whitelist and Project Skill Whitelist are ceilings: an individual agent's own OpenCode permissions can only **narrow** below the ceiling (intersection), never widen past it. `Project = trust boundary`: the ceiling is project-wide, not per-agent.
**Not:** The effective set an agent runs with (that is the ceiling *after* the agent's own narrowing), nor a per-agent setting. A Home/Identity Agent has no ceiling — ceilings apply only to a project's Config Agents.

## Project Tool Whitelist
**Definition:** A Project's UI-editable list of tools (`project.json` → `allowed_tools`) that is the hard Ceiling for its Config Agents. A new project seeds the single base-list constant `PROJECT_DEFAULT_ALLOWED_TOOLS` (`read, write, edit, glob, grep, bash, process, web_fetch, web_search, status, subagent`); other registered tools (`session_search`, `image_generation`, `text_to_speech`, `cron`, `channel_send`, Home-Assistant) are default-off but UI-toggleable. An agent's effective tools = this list minus the tools its OpenCode `permission`/`tools` deny. An empty list means every tool off.
**Not:** A per-agent allow-list, and not the effective set (the resolver still subtracts the agent's denials). `memory` and the internal tools (`skill`, `subagent_result`) are never in it — `memory` is runtime-derived from the agent's memory mode, internal tools bypass the allowlist.

## Project Skill Whitelist
**Definition:** A Project's Skill Whitelist stored as a **rule, not a resolved set** (`project.json` → `skills_bundled_enabled` + `skills_project_disabled`). The project's own skills (scanned from `<cwd>/.opencode/skills/`) are active by default; `skills_project_disabled` turns some off, and `skills_bundled_enabled` opts in named bundled skills. Effective skills = `(project skills − disabled) ∪ enabled-bundled`. Storing the rule (not a snapshot) means a newly added repo skill lights up automatically. A project skill **wins** a name collision with a bundled one.
**Not:** A flat allow-list of skill names, nor a per-agent setting (OpenCode does not narrow skills per agent in v1). Project skills are project-scoped — they never leak to other projects or the home agent.
