# Glossary

## Agent
A Session participant addressed by Agent id, supplying runtime identity and configuration. Resolves as a stored Identity Agent or repository-discovered Project Agent. Both expose the same runtime configuration surface; only Identity Agents own `agent.json` and a Workspace. Not a background process or Session.

## Agentic Loop
The chat processing cycle: the Model receives a user message, produces text and/or Tool calls, and receives Tool results until it returns a final response without Tool calls. Runs entirely in the async kernel; not an event/game loop or separate process.

## Provider
An external API service hosting AI Models, represented by an Adapter (wire-protocol code) and JSON config (base URL, authentication, Provider settings). Routes requests to Models from the layered Model DB; it is not the Model processing them. Wire/credentials: `providers.md`; Model layers: `models.md`.

## Model
An AI endpoint at one Provider, with its own ID, capabilities, and context window; the same underlying Model has a distinct entry per Provider. Users select `<provider>/<model-id>`; only the exact model-id goes on the wire. The Model processes requests; the Provider routes them. Assembly: `models.md`.

## Reasoning
A Model's capability for internal reasoning before its final answer, represented by a typed block in Model data (`reasoning.supported`). Agent `thinking_effort` steers it through Adapter-specific wire vocabulary (`models.md`, `providers.md`). The capability and configuration are distinct from Chain of Thought (CoT), its opaque output, defined in `providers.md`.

## Reasoning Replay
Returning prior Model reasoning state in later requests of the same conversation. Opaque meta (`reasoning_details`, signatures/encrypted blocks) is contract state: replay byte-identically when the wire requires it. Visible reasoning text is display material: replay only when that wire's Model demonstrably benefits. Minimal replay, not everything by default; distinct from CoT content itself. Mechanics/policies: `providers/request-policy.md`.

## Session
A system-owned persisted chat container belonging to exactly one Agent in its Identity or Project scope, with canonical Message history in `<data-dir>/sessions.db` (`sessions.md`: storage/generation rules). Distinct from its Agent, Workspace files, and active execution (Run).

## Memory
Curated durable facts in an Identity Agent's Workspace Markdown: `USER.md` for user scope, `MEMORY.md` for Agent scope. Managed by the Memory service and, when permitted, the `memory` Tool; `memory_prompt_mode` independently controls which files, if any, enter the prompt. Not scratch notes, Session history, or a broad search index; conversation recall uses Sessions and Tools such as `session_search`.

## Run
One active Session execution: a user turn and all Model output, visible thinking blocks, Tool calls/results, and follow-up assistant output until completion, failure, or cancellation. Can span multiple Model/Tool steps; not an Agent, Session, or single Provider HTTP request.

## Agent Takeover
Moving the current running Session, with the same id and full verbatim history, between Agents (personal or Team) via `/agent <addr> [task]`. Only the target retains ownership; it waits or immediately runs the optional task. No copy or summary: Handoff (`/handoff`) instead writes a summary into a fresh Session.

## Accessor
A client-facing interface to vBot (WebUI, Desktop, CLI, or future channels), communicating with the server, not directly with Providers. Not a Provider or Adapter.

## Streaming
Server-exposed incremental output from an executing Run, with Provider-specific details hidden behind Adapters. Same Run and semantics as normal send, delivered incrementally rather than only at completion; not a separate chat system.

## Cancel
A best-effort request to stop an active Run promptly: stop Model/Tool progression, attempt to abort current Provider work, ignore late results. Does not delete the Session, roll back persisted history, or erase displayed output.

## Skill
A reusable Agent playbook: `SKILL.md` teaches a task/domain workflow or convention, optionally with helper files in its directory. Activation supplies the absolute Skill directory for reading/running helpers. Distinct from a Tool that performs an operation; bundled utilities are specialized programs, not Agent Tools.

## System Reminder
A kernel-internal note persisted in a Session, then embedded in Provider requests as a synthetic user message in `<system-reminder>` tags. Background producers inform the Model without a normal user-visible chat message. Not a System Prompt, real user turn, or server/UI notification. Channel selection: `model-communication.md`.

## Tool
A function with a name, description, and parameters defined in JSON Schema, callable by an Agent during chat when Tool Access Policy and runtime conditions permit. File Tools default to **cwd** for relative paths: Project repo for Project Agents and Rooted Identity Agents, otherwise the Identity Agent's Workspace. The `memory` Tool always uses Workspace.

## Workspace
An Identity Agent's freely editable identity/Memory home: `SOUL.md`, `USER.md`, `MEMORY.md`. Supports default or custom absolute paths (`agent.md`). Always the `memory` Tool's home; cwd independently controls relative file/shell work. Not a Project selection, Session owner, or cwd synonym; equal paths do not imply Rooting.

## Project
A first-class entity with stable `project_id` slug, changeable display name, cwd (repo directory for relative Tool paths), one declared source format, auto-load files (seeded with `AGENTS.md`), project-default-agent, default-model, repo-scanned [Team](#team), and Sessions. A minimal Project needs only cwd; Team and auto-load files are optional, so an empty folder is valid. Not cwd itself, an Agent, or independently selected Workspace identity state. Team comes from the repo; runtime Project state lives in the data directory (`projects.md`).

## Team
A Project's callable Agent roster, scanned at the known location of its single source format, without mixing. Re-derived from the authoritative repo on open/explicit re-scan; a bare/empty Project normally has an empty Team. Membership is repo-/Project-scoped, not the global data-directory Agent store. Loading Project Context does not make an Identity Agent a member. Scan mechanics: `projects/scanning.md`.

## Config Agent
Workspace-less Run configuration synthesized from a scanned Project Team profile when resolving a Project Agent. No persistent identity, SOUL/USER/MEMORY home, Memory Tool, or separately stored Agent config. Model, Run settings, Skills, and Tool Access Policy resolve from repo config, Project defaults, and vBot overrides (`projects/resolution.md`). Interchangeable configuration, not an Identity Agent.

## Identity Agent
A stored Agent at `<datadir>/agents/<id>/` with `agent.json`, Workspace, and durable identity/Memory across Sessions. `memory_prompt_mode` controls Memory prompt visibility; Tool Access Policy independently controls `memory` Tool access. Unlike a Config Agent, it owns a persistent Memory home, not a workspace-less Project profile synthesized for a Run.

## Rooted Agent
An Identity Agent whose nullable saved `Project` selection names a registered Project. Retains its Workspace, Memory, private Skills, Sessions, permissions, and bare addressing; relative file/shell work, Project Files, and Project Skills use that Project. Not a Project Agent or Config Agent. Rooting does not move Session ownership, apply Project Config-Agent ceilings, or automatically expose the Team; Workspace path equality does not imply Rooting.
