# Built-in Agents: Creation, Librarian and Live Agents

Read when changing how vBot creates its built-in Agents and reports their problems, the Librarian's Skill subject binding, or the Live Agents' Tools, Models and Sessions. Fixed capabilities, lifecycle refusals and hidden surfaces are in `../agent.md` -> Built-in Agents.

## Creation and problems

- **Creation:** Runtime startup calls `AgentStore.ensure_builtin_agents()` right after `ensure_bootstrap()`; it creates each missing one through the normal create path (Workspace with the bundled `SOUL.md`, no Session yet) with a new Agent's defaults, so its Model is empty and resolves to `defaults.agent.model` until the user picks one; the Live Agents start with their default Tools (below). `builtin_problem(kind)` names why one is unavailable: `missing` (not created yet), `agent_id_taken` (an Agent of the user, or an unfinished rename, holds the id - that Agent stays untouched and keeps working) or `invalid_config`; `builtin_agent(kind)` returns it or `None`. `librarian()` / `librarian_problem()` are the Librarian's shortcuts, and `librarian_problem_message(problem)` says the problem to the user with the way out (`automation/librarian.md` -> Surfaces). Startup logs a WARNING for each unavailable one.
- **RPC payload:** `agent.get`/`agent.update` responses carry `builtin` (`librarian`, `live_voice`, `live_backend` or `null`).

## Built-in Librarian

The Librarian (GLOSSARY -> Librarian, id `librarian`, `builtin: "librarian"`) curates the other Identity Agents' own Skills in passes (`automation/librarian.md`); its Tools are only `skill` and `skill_manage`.

- **Skill subject:** a Session of the Librarian whose metadata holds `skill_agent_id` (`SKILL_AGENT_ID_KEY`) works on that Agent's Skills; the resolver applies the binding (`projects/resolution.md` -> Librarian Skill subject) and `skill_subject_id(agent)` names the Agent whose Skills any resolved Agent works on. `Agent.skill_agent_id` carries it on that runtime view only and is never persisted. An Agent rename moves the bindings that name the old id (`AgentStore._apply_rename`, `sessions/ownership.md` -> Identity-rename retargeting); an archived or deleted subject leaves them, and such a Session cannot run.

## Live Agents

`live-voice` ("Live voice", `builtin: "live_voice"`) records every Live call in a Session of its own, and `live-backend` ("Live backend", `builtin: "live_backend"`) answers the requests the voice model hands on, in a second Session of the call (`model_tasks/live.md` -> Live Agents and Sessions).

- **Tools:** a new Live Agent gets `selected` with the nine Live Tools; the backend Agent also gets `web_search` and `web_fetch` (`_BUILTIN_DEFAULT_TOOL_ACCESS`). The user changes them in Settings -> Voice -> Live voice (`agent.update` with `tool_access`). Loading grants the Live Tools and sets `live_call` (`live_agent_tool_access`), so they resolve for these Agents only (`tools.md` -> Policy resolution) and nothing beside the Tools the policy names activates. `vbot_request` is never in a policy: a call whose backend is the Live backend Agent adds it to the voice Agent's Run.
- **Models:** the voice Agent's Model settings stay unused: the `live_voice` Task Model binding picks the voice model, and the voice Session's Assistant messages record that target. The backend Agent's Runs are Chat Runs with its own Model settings.
- **Sessions:** typed input to a Live Agent is refused unless the Run kind is `live` (`chat.md` -> Invariants and conventions); Sessions with a `live` Run are listed only in the global scope of the Live Agents and never recalled (`sessions.md`).
