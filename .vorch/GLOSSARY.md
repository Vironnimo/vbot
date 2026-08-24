# Glossary

Core, cross-cutting vocabulary every agent needs regardless of what it touches. Domain-specific terms live in a `## Terms` section inside their domain map (e.g. the Model-DB internals in `models.md`, the prompt-block internals in `prompts.md`, the project/whitelist internals in `projects.md`). **A term lives in exactly one place — this file *or* one map, never both.** When you define a new term, decide by this rule: a core cross-cutting term, or one the user says in conversation → here; a term you only need once you are already working inside one domain → that domain's map.

## Agent
**Definition:** The common runtime participant in a vBot Session, addressed by an Agent id and resolved as either a stored Identity Agent or a repository-discovered Project Agent. Both forms expose the same runtime configuration surface, but only an Identity Agent owns an `agent.json` and Workspace.
**Not:** A background process or a Session. The Agent supplies runtime identity and configuration; the Session is the persisted conversation container in which it participates.

## Agentic Loop
**Definition:** The central processing cycle of a chat. The model receives a user message, responds with text and/or tool calls. If tools are called, they execute and results feed back to the model. This repeats until the model returns a final response with no tool calls. The loop runs entirely in the kernel, with streaming via SSE.
**Not:** An event loop or game loop. Not a separate process — it runs in the same async context.

## Provider
**Definition:** An external API service that hosts AI models (OpenAI, Anthropic, Groq, OpenRouter, local models via Ollama, etc.). A provider is an **Adapter** (code that speaks the wire protocol) plus a **JSON config** (`resources/providers/<name>.json`: base URL, authentication, provider-specific settings). Its models come from the layered Model DB. The wire/credential internals (Adapter, Connection, Account) live in `providers.md`; the model layers and the canonical join live in `models.md`.
**Not:** A Model. The provider is the infrastructure that routes the request; the model is the endpoint that processes it.

## Model
**Definition:** A specific AI model at a specific provider. Models are always provider-specific — the same underlying model (e.g. Claude Sonnet 4) is a distinct entry per provider, with its own ID, capabilities, and context window. The model ID is the exact string sent in the API request (`anthropic/claude-sonnet-4` at OpenRouter, `claude-sonnet-4-20250219` at Anthropic); the user selects a model as `<provider>/<model-id>`. Model data is assembled at load from layered on-disk facts — the mechanics (the canonical join, the three layers, Refresh vs Load) live in `models.md`.
**Not:** A Provider. The model is the cognitive endpoint the provider routes to; the wire model-id goes on the wire.

## Reasoning
**Definition:** A model capability for an internal reasoning step before the final answer — a typed block in the model data (`reasoning.supported`, plus how the provider steers it when supported). At runtime the agent's `thinking_effort` field (`none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`, or empty for the provider default) is turned into a provider-neutral intent, and each adapter renders that intent into its own wire vocabulary; `/status` reports what actually reaches the wire. The steering mechanics (Reasoning control in `models.md`; Reasoning intent and Chain of Thought in `providers.md`) live in those maps.
**Not:** Chain of Thought. Reasoning is the capability and its configuration; CoT is the opaque output it produces (defined in `providers.md`).

## Reasoning Replay
**Definition:** Returning a Model's prior reasoning state in later requests of the same conversation. Two classes with different rules: opaque meta (`reasoning_details`, signatures/encrypted blocks) is contract state and replays byte-identically when the wire requires it; visible reasoning text is display material and only goes back when that wire's Model demonstrably benefits.
**Not:** Chain of Thought itself (the content, not its round-trip). Also not "replay everything by default" — minimality is the invariant (see `PROJECT.md` → Context; mechanics in `providers/request-policy.md`).

## Session
**Definition:** A system-owned persisted chat container that belongs to exactly one Agent within its Identity or Project scope and owns the message history. Identity Agent Sessions live under `<datadir>/agents/<agent-id>/sessions/`; Project Agent Sessions live under `<datadir>/projects/<project-id>/agents/<agent-id>/sessions/`, with one JSONL file per Session.
**Not:** The agent itself, the currently executing work, or the agent's Workspace files. The Session is the persisted conversation container; the Run is the active execution inside it.

## Memory
**Definition:** Curated, durable facts stored in an Identity Agent's Workspace Markdown files and managed through the Memory service and, when permitted, the `memory` Tool. User-scope Memory lives in `USER.md`; Agent-scope Memory lives in `MEMORY.md`; `memory_prompt_mode` independently decides which files, if any, become prompt-visible.
**Not:** Session history, scratch notes, or a broad search index. Searchable conversation recall belongs to Sessions and recall tools such as `session_search`.

## Run
**Definition:** One active execution inside a Session: a user turn plus all model output, visible thinking blocks, tool calls, tool results, and follow-up assistant output until the work completes, fails, or is cancelled.
**Not:** The Agent, the Session, or a single provider HTTP request. A Run can span multiple model/tool steps.

## Agent Takeover
**Definition:** Moving the current running Session — full verbatim history, same id — from one Agent to another (personal or team) via `/agent <addr> [task]`, so it afterwards belongs only to the target. A persisted `agent_takeover` divider and a silent note mark the boundary; the target then waits, or runs the optional task immediately.
**Not:** A Handoff or a copy. `/handoff` writes a *summary* into a **fresh** Session; an Agent Takeover relocates the **same** Session with the literal history and no summary, and the source no longer holds it.

## Accessor
**Definition:** An external interface to the same vBot system, such as the WebUI, Desktop app, CLI, or later other channels. Accessors talk to the vBot server; they do not call providers directly.
**Not:** A Provider or Adapter. An Accessor is a client-facing entry point into vBot.

## Streaming
**Definition:** Incremental delivery of a Run's output while that Run is still executing. In vBot's external server contract, streaming is exposed by the server; provider-specific streaming details stay hidden behind adapters.
**Not:** A separate chat system with different semantics from normal send. It is the same Run, delivered incrementally instead of only at the end.

## Cancel
**Definition:** A best-effort request to stop an active Run as quickly as possible. It stops further model/tool progression, tries to abort the current provider work, and ignores late results that arrive after cancellation.
**Not:** Deleting the Session, rolling back already persisted history, or erasing output that was already shown to the user.

## Skill
**Definition:** A reusable playbook for an agent — a `SKILL.md` file with instructions that teach the agent *how* to handle a specific task or domain. A skill may optionally bundle support files under its `scripts/`, `references/`, and `assets/` subdirectories (helper programs, reference documents, templates), but most skills consist solely of the Markdown instructions. On activation the agent is told the skill's absolute directory so it can read or run those bundled files.
**Not:** A Tool. A tool does one thing; a skill teaches a workflow or convention. The utilities a skill may bundle are specialized CLI programs, not agent-tools.

## System Reminder
**Definition:** A kernel-internal note that is persisted in a Session and later embedded into a provider request as a synthetic user message wrapped in `<system-reminder>` tags. It lets background producers inform the model about events without creating a normal user-visible chat message.
**Not:** A system prompt, a real user turn, or a server/UI notification.

## Tool
**Definition:** A function with a name, a description, and a parameter schema (JSON Schema) that an Agent can call during a chat when its Tool Access Policy and runtime conditions permit it. File Tools resolve relative paths against the **cwd** by default: the Project repo for a Project Agent or Rooted Identity Agent, otherwise the Identity Agent's Workspace; the `memory` Tool always stays on the Workspace.

## Workspace
**Definition:** An Identity Agent's freely editable identity and Memory home, containing `SOUL.md`, `USER.md`, and `MEMORY.md`; its Default Workspace is `<datadir>/agents/<agent-id>/workspace/`, but a custom absolute path is valid. Workspace remains the `memory` tool's home while cwd separately controls relative file and shell work.
**Not:** A Project selection, Session owner, or synonym for cwd. Workspace and cwd may coincide, but path equality has no Rooting meaning.

## Project
**Definition:** A first-class entity (not just a cwd), keyed by a stable `project_id` slug with a changeable display name, that bundles a cwd (the repo directory tools resolve relative paths against), a single [[Source Format]] (which coding-agent ecosystem its Team and skills come from), an auto-load file list (`AGENTS.md` is seeded as its first entry at creation, then a normal removable entry), a project-default-agent, a default-model, a team scanned live from the repo, and Sessions. The minimal Project is just a cwd — team and auto-load files are all optional, so an empty folder is a valid Project.
**Not:** A bare cwd, a Workspace, or an Agent. The cwd is one field of a Project; Workspace is independently selected identity state. vBot reads the repo to discover the Team but stores runtime Project state in the data-dir; details live in `projects.md`.

## Team
**Definition:** The set of agents discovered in a Project by the **scan** of the repo, at the known location of the project's single [[Source Format]] only — no mixing. OpenCode reads `.opencode/agents/` non-recursively; Claude Code reads `.claude/agents/` recursively **within that directory only** (subfolders allowed, never a repo tree walk). The team is the project's roster of callable agents; it is re-derived from the repo on open / explicit re-scan (the repo is the source of truth, no copy drift). A bare/empty project has an empty team — that is normal, not an error.
**Not:** The global Agent store. Team membership is project-scoped and lives in the repo, not in the data-dir agent store. An Identity Agent with explicitly loaded Project Context is **not** a team member.

## Config Agent
**Definition:** The workspace-less runtime representation synthesized when a Project Agent is resolved from its scanned Project Team profile, with no identity or Memory Tool. Its model, Run settings, Skills, and Tool Access Policy resolve from repository configuration plus Project defaults and vBot overrides; a vBot Tool override fully replaces the repository Tool policy inside the Project Tool Whitelist, so it may narrow access to one Tool or re-enable a repo-denied Tool.
**Not:** An Identity Agent or a separately stored Agent configuration. A Config Agent has no SOUL/USER/MEMORY home and is interchangeable Run configuration rather than a persistent identity.

## Identity Agent
**Definition:** A stored Agent under `<datadir>/agents/<id>/` with its own `agent.json`, Workspace, durable identity, and Memory files across Sessions. The Workspace provides its Memory home, while `memory_prompt_mode` and the Tool Access Policy independently decide prompt visibility and whether the `memory` Tool is callable.
**Not:** A Config Agent. The Identity Agent is the persistent self with a Memory home; a Config Agent is a workspace-less Project profile synthesized for a Run.

## Rooted Agent
**Definition:** An Identity Agent whose nullable saved `Project` selection names a registered Project. It keeps its own Workspace, Memory, private Skills, Sessions, permissions, and bare addressing while relative file/shell work, Project Files, and Project Skills use the selected Project.
**Not:** A Project Agent or Config Agent. Rooting does not move Session ownership, apply Project Config-Agent ceilings, expose the Project Team automatically, or derive from Workspace path equality.

## Project Context
**Definition:** The current instructions, absolute Project path, and available Project Skills that tell an Agent how to work in a registered Project. It is supplied automatically when that Project is the Agent's working Project or loaded explicitly by an Identity Agent through the `project` Tool.
**Not:** An Agent type, Project membership, Rooting, or a current-working-directory change; an explicit load changes none of those, and every one-shot `bash` call must set `workdir` again.

## Source Format
**Definition:** A Project's single declared coding-agent ecosystem (`project.json` → `source_format`, one of `opencode` / `claude`), which bundles **both** where its [[Team]] agents come from **and** where its project skills come from: `opencode` = `.opencode/agents/` + `.opencode/skills/`; `claude` = `.claude/agents/` + `.claude/skills/`. Exactly one per project — no mixing (same-named agents/skills across ecosystems are usually the same tool tuned per harness, so auto-merging would silently discard one copy). Every consumer (team scan, config-agent tool/skill resolution, rooted-identity skill catalog, explicit Project Context loading, `$`/`/` autocomplete, prompt preview) sees only this format's set. Auto-detected at project creation (exactly one present → that one; both/neither → `opencode`), changeable later in the project settings (sessions survive; team and skills re-derive live from the repo). Default `opencode`; an old `project.json` without the field loads there. **Context files stay format-independent:** `AGENTS.md` is the seeded auto-load convention for every project; `CLAUDE.md` is never auto-loaded and its `@import` semantics are never emulated (the add dialog only *suggests* it as a normal project file when no `AGENTS.md` exists).
**Not:** The per-member `source_format` provenance tag on a scanned [[Team]] member (`ScannedAgent.source_format` — which detector produced it). Same vocabulary and value set, but that is a member-level fact recorded by the scan, while the Source Format is the **project-level** choice that decides which detector runs at all.
