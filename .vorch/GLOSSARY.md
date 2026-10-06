# Glossary

What vBot's shared terms mean and what they are not, so they are not confused with the coding agent's own environment or other systems. How a concept currently works lives in its domain map.

## Agent
A Session participant that vBot runs: it supplies the identity, instructions and runtime configuration (Model, Tools, Skills) for a Session's Runs. vBot has Identity Agents, Project Agents (Config Agents of a Project's Team) and temporary Agents managed by an Extension (`agent.md`). Not the coding agent working on this repository, a background process, or a Session.

## Agentic Loop
The chat processing cycle: the Model receives a user message, produces text and/or Tool calls, and receives Tool results until it answers without Tool calls (`chat.md`). Not an event loop, game loop or separate process.

## Provider
An external API service that hosts Models and routes requests to them, connected through vBot's Provider configuration (`providers.md`). Not the Model that processes the requests.

## Model
An AI endpoint at one Provider, with its own id, capabilities and context window; the same underlying Model has a separate entry per Provider and is selected as `<provider>/<model-id>` (`models.md`). Not the Provider that hosts it.

## Reasoning
A Model's capability to reason internally before its final answer, which an Agent's thinking effort steers where the Model supports it (`models.md`, `providers.md`). Not Chain of Thought (CoT), the reasoning output itself (`providers.md`).

## Reasoning Replay
Returning a Model's earlier reasoning state in later requests of the same conversation, where the Provider requires it or the Model benefits (`providers/request-policy.md`). Not the Reasoning capability or Chain of Thought itself.

## Archive Entry
The restorable unit a delete creates: deleting an Identity Agent, Project or Session archives it with its Sessions and files instead of erasing it, and the entry is later restored or purged as a whole (`archive.md`). Not a hidden live resource, a compressed file, or a deleted Skill, which moves into its Skill home's own archive (`skills.md` -> Skill Archive).

## Session
A persisted conversation belonging to exactly one Agent, holding its Message history (`sessions.md`). Distinct from its Agent, Workspace files and an active execution (Run). Not the coding agent's own session or a login session.

## Memory
Curated durable facts about the user and the work, kept as Markdown in an Identity Agent's Workspace (`USER.md` for the user, `MEMORY.md` for the Agent) and available to the Agent through its prompt or the `memory` Tool (`memory.md`). Not scratch notes, Session history, or a search index; recalling earlier conversations uses Sessions and Tools such as `session_search`.

## Run
One Session execution with a durable identity and lifecycle: an initiating request, any User messages explicitly steered into it, and all Model output, visible thinking blocks, Tool calls/results, and follow-up assistant output until completion, failure, interruption, or cancellation (`runs.md`). Can span multiple Model/Tool steps; not an Agent, Session, or single Provider HTTP request.

## Agent Takeover
Moving the current Session, with its id and full history, to another Agent via `/agent <addr> [task]`; the target owns it from then on and runs the optional task (`subagents.md`). Not Handoff (`/handoff`), which writes a summary into a fresh Session.

## Accessor
A client-facing interface to vBot (WebUI, Desktop, CLI, or Channels), communicating with the server, not directly with Providers. Not a Provider or Adapter.

## Streaming
Incremental output from an executing Run as the server delivers it to Accessors. Same Run and semantics as a normal send, delivered while it happens; not a separate chat system.

## Cancel
A best-effort request to stop an active Run promptly. Does not delete the Session, roll back persisted history, or erase displayed output.

## Skill
A reusable Agent playbook: a `SKILL.md` that teaches a task, domain workflow or convention, optionally with helper files in its directory, which an Agent loads when it needs it (`skills.md`). Not a Tool, which performs an operation, and not a skill of the coding agent's own harness.

## System Reminder
A note vBot adds to a Session to inform the Model without a visible user message; the Model receives it as a user message in `<system-reminder>` tags (`model-communication.md`). Not a System Prompt, a real user turn, a server or UI notification, or the coding agent's own system reminders.

## Tool
A function vBot offers its Agents, with a name, description and JSON Schema parameters, callable during a Run when the Agent's Tool access allows it (`tools.md`). Not a tool of the coding agent's own harness.

## Workspace
An Identity Agent's own home for its identity and Memory files (`SOUL.md`, `USER.md`, `MEMORY.md`) (`agent.md`). Not a Project, the directory a Session's file and shell work uses, or a Session owner; a Workspace path that equals a Project path does not select that Project.

## Project
A registered repository that Agents work in, with ordered repository Sources for instructions, Skills and its Team, plus defaults and capability ceilings; runtime Project state lives in vBot's data directory, not in the repository (`projects.md`). Not the repository directory itself, an Agent, or a Workspace.

## Team
A Project's addressable Agents combined from active repository Sources in priority order; the first definition of each name wins and later definitions are shadowed (`projects.md`, `projects/scanning.md`). Membership belongs to the Project, not to vBot's stored Agents; loading Project Context does not make an Identity Agent a member.

## Config Agent
The runtime configuration vBot builds for a Team member from its repository definition, Project defaults and overrides (`projects/resolution.md`). It has no Workspace, Memory or stored identity of its own. Not an Identity Agent.

## Identity Agent
An Agent that vBot stores with its own configuration, Workspace and Memory, which persist across Sessions (`agent.md`). Not a Config Agent or a temporary Agent.

## Librarian
vBot's built-in hidden Identity Agent that keeps other Identity Agents' own Skills small and current, in passes the user can open as Sessions (`agent.md`, `automation.md` -> Librarian). Not a Tool, a Reflection, or a user's Agent that happens to be named "librarian".

## Default Project
The Project in which an Identity Agent's new Sessions work unless their creator names another Project or the Workspace ("Default project" in the WebUI). Changing it moves no existing Session and changes nothing else about the Agent (`agent.md`, `sessions.md` -> Terms -> Working Project). Not Team membership. Code and stored names keep the older term Rooting (`root_project_id`, `agents_rooted_in`, `unrooted_agents`, `--copy-rooted-agent-files`).
