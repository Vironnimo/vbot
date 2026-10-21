# Subagents

Sub-Agent Sessions: the durable Parent-to-Sub-Agent tree, forwarding of Sub-Agent answers to the Parent, tree control, and the Sub-Agent's side of the conversation.

## Overview

`core/subagents/` owns the runtime behavior behind the `subagent` and `message_parent` Tools and the Sub-Agent branch of `background_tasks`; their definitions, schema, call-syntax normalization and display live in `core/tools/subagent.py` (`tools/subagent.md`) and `core/tools/background_tasks.py`. `SubAgentCoordinator` (`subagents.py`) handles the `subagent` actions `run` and `send`, `message_parent`, what `background_tasks` lists, checks and stops (`subagents`, `running_work`, `stop_subagent`), the user's "Stop all" (`stop_tree`) and the WebUI projections (`inspect`, `subagent.tree`; `subagents/tree-control.md`). Internal parts: `_tree.py` owns live tree observation and whole-subtree cancellation under the coordinator's shared admission lock; `_delegation.py` owns target validation, Session setup and operation projections; `_types.py` holds tree read/control contracts; `links.py` answers every tree question from durable Session metadata; `forwarding.py` follows Runs in Sub-Agent Sessions, forwards their answers and owns the activity files; `activity.py` writes one activity file; `catalog.py` projects prompt targets; `_interpretation.py` settles what a call addresses and names valid choices; `_constants.py` holds policy defaults, event names and the domain's Agent-facing results, refusals and sections.

Not owned here: coalescing and waking for deliveries (the automation completion coordinator, `automation.md`); selecting the reply target for incoming messages (Chat persistence, see Conventions); the Stop commands themselves (`/stop` in `core/chat/commands.py`, the `chat.cancel` / `chat.stop_all` RPCs in `server/rpc/chat_methods.py`); System Prompt block mechanics (`prompts.md`).

## Terms

Core terms (Agent, Session, Run, Queue, System Reminder) live in `.vorch/GLOSSARY.md`.

### Sub-Agent
A Session linked to the Parent Session that started it with `run`, together with the Agent working in it. It keeps one public id (`sub_` plus 16 lowercase base32 characters, `new_id("sub")`) for its whole life; every `send` and every `background_tasks` call on it uses that id.

### Parent Session / Parent Agent
The Session (and its Agent) whose `run` created a Sub-Agent. A Sub-Agent can itself be a Parent.

### Agent tree
A root Session that is not a Sub-Agent plus every Sub-Agent below it. Stop all and `background_tasks` stop act on complete subtrees, regardless of who receives their answers; the active limits count per Session and app-wide, not per tree.

### Forwarding
Delivering the final answer of a Sub-Agent's turn to its Parent Session as a completion section, which wakes an idle Parent.

### Reply target
The recipient of a Sub-Agent's final answers: Parent or user. User input selects the user; Parent input or an accepted `message_parent` call selects the Parent again. This never changes the Parent link, messaging, visibility, limits or stop controls (user decision 2026-10-14, replacing the former takeover boundary). Automatic continuations retain the selected recipient.

## Data Model

- **The Parent link is the only tree state.** `_open_subagent_session` creates the Session with its `auto_title`, `is_subagent_session: true` and `subagent_parent` `{id, agent_id, session_id, project_id, run_id, tool_call_id, tool_call_index}` (the Parent address plus the creating call) and Session Agent overrides in one transaction on the Session database's pool, so cancellation or a failed metadata write cannot leave an unlinked Session. Its first Run must admit that exact generation before success is reported; admission failure discards only that unused generation under a Run admission guard, preserving a replacement or used Session. The Session store projects the link into `subagent_parent_*` columns; two partial indexes back `ChatSessionManager.subagent_children(parent)` (live children, oldest first) and `subagent_session(id)` (the live Session with that public id). Forks strip the Sub-Agent marker, Parent link, reply target and historical takeover timestamp.
- `links.py` answers children, descendants (breadth first), ancestors (at most 64) and id lookup from durable links; callers use `sessions.run_async`. Descendant walks visit each Session once, so cycles cannot repeat work, and always cover the complete subtree regardless of reply target or historical size. `background_tasks` list shows at most 1000 Sub-Agent rows. The tree survives restarts without an in-memory registry; a deleted Session and its descendants become unreachable from the root.
- `read_link` and `ancestors` accept `strict=True`: a missing Session still ends the chain, but any other metadata-read failure propagates. Scratch Areas use this mode for placement and retention, so unavailable ancestry cannot turn a busy shared area into a deletion candidate (`tools/scratch.md`; `test_runtime_wiring.py`). The default remains tolerant for existing tree readers.
- `subagent_reply_target` is `parent` (the default for linked Sessions) or `user`. Chat commits a changed target with the input through `append_many` / `apply_edit`; an accepted `message_parent` changes it under the Session write lock. Sessions stamp the current target on each new entry and the terminal Run summary (`entries.subagent_reply_target`), so later input cannot redirect earlier answers or a completed Run. The field never enters Provider requests or public Message payloads. The one-time migration `sessions.0003_subagent_reply_targets` selects `user` for linked Sessions with historical `subagent_taken_over_at` and no explicit target; the old timestamp no longer affects control.
- Title: the whitespace-normalized `description`, at most 48 characters, set at creation and never changed by the coordinator; automatic title generation skips Sub-Agent Sessions (`core/sessions/titles.py`).
- Process-local state only: one coordinator-wide admission lock (`_admission_lock`, held through limit checks and Run admission, and through a stopped tree's lookup and cancellation signals), the followed Runs and silenced Run ids in `SubAgentForwarding`, and the activity file per Session in `SubAgentActivities`.
- **Activity file:** one Markdown file per Sub-Agent Session under `<data_dir>/artifacts/temp/subagents/` records each followed Run's visible output, Tool lines and Parent delivery states; it serves observability only, and its failures never affect delivery (`subagents/forwarding.md` -> Activity file).

## Interfaces

- `SubAgentCoordinator(runtime, trigger_service, *, secrets=None)` (`secrets`: `Runtime.agent_secrets`); `install(run_manager)` registers forwarding as a Run-started callback. Bootstrap (`core/runtime/_bootstrap.py`) passes `runtime._chat_run_manager` because the started-service accessors raise before the Runtime has started.
- `spawn(context, arguments)` handles `subagent` `run` and `send` (the Tool runs its former `list` and `cancel` words as `background_tasks`, `tools/subagent.md`); `message_parent(context, arguments)` is the `message_parent` handler.
- `background_tasks` reads the tree through `subagents(caller)` (all descendants, breadth first, as frozen `SubAgentState`), `running_work(subagent)` and `stop_subagent(context, subagent) -> SubAgentStopOutcome` (`stopped` or `idle`). These form `SubAgentTree`, implemented by `SubAgentCoordinator`; `background_tasks` and `terminal` receive it from Runtime and import it for annotations only. The Tools own ids, texts and row layout.
- `stop_tree(address) -> int` is "Stop all" (`/stop all`, `chat.stop_all`, WebUI); returns how many Runs, queued items, commands and terminals it stopped across the complete subtree.
- `work_snapshot(address, include_entries=False)` is the live control projection served by `chat.work`: `run_active` for the root and `background_work_running` for stoppable commands, Terminal ownership/attachments and descendant Runs/Queues. It reads durable links on the Sessions worker independently of loaded History, includes pending resource launches, and excludes the root Queue that Stop all retains. Tree reads for this projection and stopping are strict: an unavailable descendant cannot be mistaken for an idle or complete subtree. `chat.work` adds a neutral `running` entry list with `include_entries=True` (`subagents/tree-control.md` -> Live work entries).
- Chat's hooks (`ChatLoopDependencies`): `subagent_reply_to_user(address, message_id)` for a switch to user replies; `subagent_tool_usable(address)`, whether a Sub-Agent Session gets the `subagent` Tool (not at the nesting limit, `subagents/delegation.md` -> Limits).
- `inspect(agent_id, session_id, subagent_id, project_id=None)` returns the WebUI projection of one Sub-Agent Session (`subagents/tree-control.md` -> inspect); `chat_methods.py` serves it.
- `references_identity_agent(agent_id)` guards Identity rename while a followed Run in that Identity's Session is still forwarding.
- `prompt_targets(agent, project_id)` feeds the `tool:subagent` block: each target with its Tool-call id, display name and description (an Identity Agent's user-written `description`, `agent.md`; a Team member's repository description), empty when none; `drain_activity()` is for shutdown.
- Events (WebUI, through `ToolContext.emit`): `subagent_session_started` on `run` and `send`, `subagent_status_changed` (status `cancelled`) when `stop_subagent` stopped something (payloads: `subagents/tree-control.md`). Events and metadata keep bare ids; Agent-facing results use the qualified address.
- Sub-Agent Runs are admitted with `RunKind.SUBAGENT`, `work_id` = public id, `owner` = the caller's execution owner, and count toward Agent activity only without an owner. The executor is `chat_loop.run_executor(content, parent_agent_input=True, temporary_parent_binding=...)`, which is steerable.
- Deliveries carry `SessionAddress` intact through `TriggerService.submit_completion` under the Run's execution owner; forwarded answers use notice id `subagent:<run_id>`, and a switch to user replies sends a non-waking notice (`subagents/forwarding.md` -> Delivery notices).

## Conventions

- **Secrets:** a Sub-Agent Session uses only the Agent Secrets bound to or passed into it, also when it runs its Parent's own Agent (user decision, 2026-10-21). `run` takes `secrets` (names), checked with `AgentSecrets.passable` before any Session work; the new Session is then added to each Secret, and the note `SUBAGENT_SECRETS_NOTE_TEMPLATE` lists them there (`secrets/agent-secrets.md` -> Lifetimes and grants).
- **Settings capture:** a new Sub-Agent captures its Model and thinking effort in its Session metadata at start (self-delegation: the running Parent's actual settings); later Parent or default changes never alter it (`subagents/delegation.md` -> Model and settings capture).
- **`run` and `send`:** `run` always starts a new Sub-Agent in the background and returns at once, with every check before any Session work; `send` reaches only a direct child and refuses a stale generation instead of recreating or redirecting (`subagents/delegation.md`).
- **Targets:** the coordinator is the security boundary for which Agents and Projects a caller may start; Provider-schema narrowing and the prompt block are guidance (`subagents/delegation.md` -> Targets).
- **Limits:** `max_subagent_depth`, `max_active_subagents` (the caller's own working Sub-Agents) and `max_active_subagents_total` (the process) are checked under the coordinator-wide admission lock; every working Sub-Agent counts regardless of reply target (`subagents/delegation.md` -> Limits).
- **Forwarding:** when a followed Run in a linked Session ends, its Parent-directed answers under that Run id reach the Parent as one completion section, which wakes an idle Parent; target changes after persistence never redirect earlier answers (`subagents/forwarding.md`).
- **Parent framing:** Parent input reaches the Sub-Agent with `PARENT_AGENT_INPUT_SYSTEM_REMINDER` at a turn start or `PARENT_AGENT_STEERING_SYSTEM_REMINDER` when steered (`core/chat/messages.py`). The `core:subagent_role` System Prompt block (owner `subagent_session`, text `resources/prompts/subagent_role.md`) and the session-scoped `message_parent` grant (`SUBAGENT_SESSION_TOOL_NAMES`) apply exactly when the Session has a Parent link; Chat reads that flag per Run context and at Compaction, regardless of the reply target.
- **Recipient changes:** Chat selects `parent` for Parent input and `user` for other external input (`subagent_input_reply_target`), ordered with input persistence by the Session write lock; an accepted `message_parent` restores Parent replies. The Parent link and all controls stay intact (`subagents/forwarding.md` -> Recipient changes).
- **Stop (every Session and Channel):** Run cancellation stops only the current Run and the shell commands still inside its calls (`CommandManager.cancel_run`, `tools/shell.md`); background commands, terminals and Sub-Agents keep working and deliver later, waking the Session. That is intended: Stop all (`stop_tree`) and `background_tasks` stop are the controls that end complete subtrees (`subagents/tree-control.md`).
- **Execution ownership** (temporary Agents, extension groups): Sub-Agent Runs and queued sends carry the caller's owner, forwarded sections carry the Run's owner, and a copy of a temporary caller runs under its temporary parent binding without the private Session grants. A closed owner drops its deliveries silently (`RunAdmissionBlockedError`, DEBUG). `tests/core/agents/test_temporary.py` covers actual Chat execution.

## Constraints & Gotchas

- Forwarding and activity state are process-local: a Run is followed only when it starts in this process (Runs do not survive restarts either), and an activity file allocated before a restart is not reused.
- Tests: `tests/core/subagents/subagents_test_support.py` dispatches through the registered Tools (`subagent`, `message_parent`, `background_tasks` over a `CommandManager`) over real Sessions and a real `ChatRunManager`; only the Agent resolver, the completion coordinator and the Chat loop are doubles. Coverage: `tests/core/subagents/test_subagents.py`, `tests/core/tools/test_subagent.py`, `tests/core/tools/test_background_tasks.py` (Sub-Agent rows, status and stop texts against a fake tree).

## Agent-facing text

This domain owns the Sub-Agent role block (`resources/prompts/subagent_role.md`, a Storage-allowlisted prompt fragment, `storage/prompt-files.md`), the reminders in `core/chat/messages.py`, and in `_constants.py` the delegation Tool start and send results, activity receipts, forwarded sections, the Secret pass-through note, and the target, depth and limit refusals (with the `secrets` refusals in `subagents.py`). Before you change, add or delete a sentence of them, read `subagents/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing `run` or `send` admission, settings capture, cancellation admission, the active limits or target authorization -> `subagents/delegation.md`
- Changing how answers reach the Parent, reply-target switching, `message_parent`, delivery notices or the activity file -> `subagents/forwarding.md`
- Changing `background_tasks` list, status or stop for Sub-Agents, Stop all, `chat.work` entries, `inspect`, `subagent.tree` or the Sub-Agent events -> `subagents/tree-control.md`
- Changing the `subagent` or `message_parent` definitions, call syntax, display or the `tool:subagent` block -> `tools/subagent.md`
- Changing how deliveries coalesce, wake or persist -> `automation.md`
