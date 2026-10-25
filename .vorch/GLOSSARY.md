# Glossary

What vBot's shared terms mean and what they are not, so they are not confused with the coding agent's own environment or other systems. How a concept currently works lives in its domain map.

## Agent
A Session participant that vBot runs: it supplies the identity, instructions and runtime configuration (Model, Tools, Skills) for a Session's Runs. vBot has Identity Agents, Project Agents and temporary Agents managed by an Extension (`agent.md`). Not the coding agent working on this repository, a background process, or a Session.

## Agentic Loop
The chat processing cycle: the Model receives a user message, produces text and/or Tool calls, and receives Tool results until it answers without Tool calls (`chat.md`). Not an event loop, game loop or separate process.

## Provider
An external API service that hosts Models and routes requests to them, connected through vBot's Provider configuration (`providers.md`). Not the Model that processes the requests.

## Model
An AI endpoint at one Provider, with its own id, capabilities and context window; the same underlying Model has a separate entry per Provider (`models.md`). Not the Provider that hosts it.

## Reasoning
A Model's capability to reason internally before its final answer, which an Agent's thinking effort steers where the Model supports it (`models.md`, `providers.md`). Not Chain of Thought (CoT), the reasoning output itself (`providers.md`).

## Archive Entry
The restorable unit a delete creates: deleting an Identity Agent, Project or Session archives it with its Sessions and files instead of erasing it, and the entry is later restored or purged as a whole (`archive.md`). Not a hidden live resource, a compressed file, or a deleted Skill, which moves into its Skill home's own archive (`skills.md` -> Skill Archive).

## Session
A persisted conversation belonging to exactly one Agent, holding its Message history (`sessions.md`). Distinct from its Agent, Workspace files and an active execution (Run). Not the coding agent's own session or a login session.

## Ephemeral Session
A throwaway Session the user asked for when starting it (`vbot chat --ephemeral`): it is never the Agent's current Session, never listed, recalled or reviewed for learning, and deleted permanently a fixed time after its last activity (`sessions/lists-and-metadata.md` -> Ephemeral Sessions). Not a temporary Session of an Extension-managed Agent, and not a Scratch Area.

## Sub-Agent
A Session that another Session started through the `subagent` Tool to work on a task and report back, together with the Agent working in it; the starting Session is its Parent Session (`subagents.md`). Not the coding agent's own subagents.

## Memory
Curated durable facts and preferences about the user and the work, owned by an Identity Agent or shared by a Project and available through the Agent's prompt or the `memory` Tool (`memory.md`). Not scratch notes, Session history, or a search index; recalling earlier conversations uses Sessions.

## Run
One Session execution with a durable identity and lifecycle: an initiating request, any User messages steered into it, and all Model output and Tool calls until it completes, fails, is interrupted or is cancelled (`runs.md`). Can span multiple Model/Tool steps; not an Agent, Session, or single Provider HTTP request.

## Queue
A Session's ordered waiting work: messages and other Run requests that arrive while the Session is busy wait there and start as Runs one after another (`runs.md`). Not a background job system or a Provider's request queue.

## Steer
Delivering a queued User message into the Session's active Run, where the Model sees it at its next request, instead of waiting for a Run of its own (`runs/queue-and-admission.md` -> Steering). Not a new Run, and not a Cancel.

## Session activity
Whether a Session's work is finished, as the server reports it to every Accessor; a Session without a Run that still waits for work that will wake it by itself, such as Sub-Agents answering it, is not finished (`activity.md`). Not Agent activity (whether a Run counts toward an Agent's attention status), the background job list, or a Sub-Agent's activity file.

## Agent Takeover
Moving the current Session, with its id and full history, to another Agent with the `/agent` command; the target owns it from then on (`chat/commands.md`). Not Handoff (`/handoff`), which writes a summary into a fresh Session.

## Accessor
A client-facing interface to vBot (WebUI, Desktop, CLI including its TUI, or Channels), communicating with the server, not directly with Providers. Not a Provider or Provider Adapter.

## Access Mode
Which clients a vBot server admits and whether they must sign in: `local`, `lan` or `authenticated`, chosen by the host for each server start (`access.md`). Not an Agent's Tool access, a Project's capability ceiling, or the server's startup mode.

## Device
A paired client installation, such as a browser profile, a Desktop app or a CLI, that the host or another Device approved to sign in to a server in the Access Mode `authenticated`, with its own revocable token (`access.md`). Not an Accessor kind, a Client in the server's presence registry, a Channel, or the computer itself: one computer can hold several Devices.

## Streaming
Incremental output from an executing Run as the server delivers it to Accessors. Same Run and semantics as a normal send, delivered while it happens; not a separate chat system.

## Cancel
A best-effort request to stop an active Run promptly. Does not delete the Session, roll back persisted history, or erase displayed output.

## Skill
A reusable Agent playbook: a `SKILL.md` that teaches a task, domain workflow or convention, optionally with helper files in its directory, which an Agent loads when it needs it (`skills.md`). Not a Tool, which performs an operation, and not a skill of the coding agent's own harness.

## System Reminder
A note vBot adds to a Session to inform the Model without a visible user message; the Model receives it as a user message in `<system-reminder>` tags (`model-communication.md`). Not a System Prompt, a real user turn, a server or UI notification, or the coding agent's own system reminders.

## Prompt epoch
The stretch of a Session from its start or a successful Compaction to the next one, during which the System Prompt and the Tool list stay fixed so the Provider prompt cache holds (`chat/request-building.md`). Not a Run, which is one turn inside an epoch.

## Tool
A function vBot offers its Agents, with a name, description and JSON Schema parameters, callable during a Run when the Agent's Tool access allows it (`tools.md`). Not a tool of the coding agent's own harness.

## Permission Rules
An Agent's optional rule set that limits which paths its Tools may read or change, which shell commands they may run and which hosts they may reach; an Agent without one has full access (`permissions.md`). Not the Tool Access Policy (which Tools an Agent has), the server's Access Mode, or an OS sandbox.

## Agent Secret
A value such as a password, API key or login that the user gives an Agent to use by reference without the Agent seeing it; vBot inserts the value only where a Tool accepts it (`secrets.md`). Not the Secret Store's credentials or OAuth tokens, such as Provider keys, which vBot itself uses and no Agent can reference.

## Workspace
An Identity Agent's own home for its identity and Memory files (`agent.md`). Not a Project, even when its path equals one, and not a Session owner; a Session of an Identity Agent works in it only when it has no Working Project.

## Project
A registered repository that Agents work in, with its Sources, Team, defaults and capability ceilings; vBot keeps the Project's runtime state in its data directory, not in the repository (`projects.md`). Not the repository directory itself, an Agent, or a Workspace.

## Working Project
The Project a Session works in for its whole life: its file and shell work, Project Skills and Project Context come from that repository (`sessions/ownership.md`). Not the Default Project, which only seeds new Sessions, and not the Session's addressing Project, which stays empty for an Identity Agent's Session working in a Project.

## Project Context
The instructions, Project path, Project Skills and enabled Project Memory that tell an Agent how to work in a registered Project, supplied for its Working Project or loaded explicitly by an Identity Agent through the `project` Tool (`projects.md`, `tools/project.md`). Not Team membership, a Working Project, or a change of the working directory.

## Scratch Area
The temporary area in vBot's data directory for one conversation and its Sub-Agent tree, with an own Scratch folder for each participating Session (`tools/scratch.md`). Not a Workspace, Memory, a delivery location for the user, or the coding agent's own scratchpad directory.

## Team
A Project's addressable Agents, combined from the Project's active Sources (`projects.md`, `projects/scanning.md`). Membership belongs to the Project, not to vBot's stored Agents; loading Project Context does not make an Identity Agent a member.

## Project Agent
An Agent of a Project's Team, which vBot resolves at runtime from its repository definition, Project defaults and overrides; it has no Workspace, personal Memory or stored identity of its own (`projects/resolution.md`). Code and older notes call it Config Agent. Not an Identity Agent.

## Identity Agent
An Agent that vBot stores with its own configuration, Workspace and Memory, which persist across Sessions (`agent.md`). Not a Project Agent or a temporary Agent.

## Built-in Agent
An Identity Agent vBot creates and keeps itself and hides from the user's roster, such as the Librarian (`agent.md` -> Built-in Agents). Not a bundled template, a Project Agent, or a user's Agent that happens to use the same name.

## Librarian
vBot's built-in hidden Identity Agent that keeps other Identity Agents' own Skills small and current, in passes the user can open as Sessions (`agent.md`, `automation.md` -> Librarian). Not a Tool, a Reflection, or a user's Agent that happens to be named "librarian".

## Default Project
The Project in which an Identity Agent's new Sessions work unless their creator names another Project or the Workspace (`agent.md`). Not Team membership; code calls it Rooting (`agent.md` -> Terms -> Rooting).
