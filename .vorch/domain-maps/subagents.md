# Subagents

Sub-Agent Sessions: the durable Parent-to-Sub-Agent tree, forwarding of Sub-Agent answers to the Parent, tree control, and the Sub-Agent's side of the conversation.

## Overview

`core/subagents/` owns the runtime behavior behind the `subagent` and `message_parent` Tools; their definitions, schema, call-syntax normalization and display live in `core/tools/subagent.py` (`tools/subagent.md`). `SubAgentCoordinator` (`subagents.py`) handles the four `subagent` actions, `message_parent`, the user's "Stop all" (`stop_tree`) and the WebUI projection (`inspect`). Internal parts: `links.py` answers every tree question from durable Session metadata; `forwarding.py` follows Runs in Sub-Agent Sessions, forwards their answers and owns the activity files; `activity.py` writes one activity file; `catalog.py` projects prompt targets; `_interpretation.py` settles what a call addresses and names valid choices; `_constants.py` holds policy defaults, event names and the domain's Agent-facing results, refusals and sections.

Not owned here: coalescing and waking for deliveries (the automation completion coordinator, `automation.md`); detecting the user's takeover (Chat persistence, see Conventions); the Stop commands themselves (`/stop` in `core/chat/commands.py`, the `chat.cancel` / `chat.stop_all` RPCs in `server/rpc/chat_methods.py`); System Prompt block mechanics (`prompts.md`).

## Terms

Core terms (Agent, Session, Run, Queue, System Reminder) live in `.vorch/GLOSSARY.md`.

### Sub-Agent
A Session linked to the Parent Session that started it with `run`, together with the Agent working in it. It keeps one public id (`sub_` plus 12 lowercase base32 characters) for its whole life; every `send`, `list` and `cancel` uses that id.

### Parent Session / Parent Agent
The Session (and its Agent) whose `run` created a Sub-Agent. A Sub-Agent can itself be a Parent.

### Agent tree
A root Session that is not a Sub-Agent plus every Sub-Agent below it. `max_active_subagents` counts per tree; Stop all and `cancel` act on subtrees.

### Forwarding
Delivering the final answer of a Sub-Agent's turn to its Parent Session as a completion section, which wakes an idle Parent.

### Takeover
The user's first own message in a Sub-Agent Session. It permanently ends forwarding and Parent messaging for that Session.

## Data Model

- **The Parent link is the only tree state.** `_open_subagent_session` creates the Session, sets its `auto_title`, writes `is_subagent_session: true` and `subagent_parent` `{id, agent_id, session_id, project_id, run_id, tool_call_id, tool_call_index}` (the Parent address plus the creating call) and stores Session Agent overrides, as one blocking unit on the Session database's pool, so a cancelled Parent never leaves an unlinked Session. The Session store projects the link into `subagent_parent_*` columns; two partial indexes back `ChatSessionManager.subagent_children(parent)` (live children, oldest first) and `subagent_session(id)` (the live Session with that public id). Forks strip all three Sub-Agent keys.
- `links.py` answers children, descendants (breadth first, at most 1000), ancestors (at most 64) and id lookup from those links; all functions block, so callers use `sessions.run_async`. The tree therefore survives restarts and needs no in-memory registry; a deleted Sub-Agent Session drops out of every walk, and so do its own children (no longer reachable from the root).
- `subagent_taken_over_at` (ISO 8601 UTC) is set once by `ChatSessionManager.mark_subagent_taken_over`, which records it only for a linked Session and returns whether this call recorded it.
- Title: the whitespace-normalized `description`, at most 48 characters, set at creation and never changed by the coordinator; automatic title generation skips Sub-Agent Sessions (`core/sessions/titles.py`).
- Process-local state only: a start lock per tree root (`_start_locks`, held from the tree read through the started Run, so concurrent `run` calls cannot pass the limit check on a stale tree; dropped when no start uses it), the followed Runs and silenced Run ids in `SubAgentForwarding`, and the activity file per Session in `SubAgentActivities`.
- **Activity file:** one Markdown file per Sub-Agent Session under `<data_dir>/artifacts/temp/subagents/`, created on first use (by `run`, `send`, or the first followed Run in this process) and recording every followed Run in start order. It holds visible Assistant output (deltas live, finalized output as fallback) and one line per Tool call with name, display summary and state; user and Parent messages, Reasoning, Tool arguments and results never enter it. The lease is held while a Run is followed and finished after it; the next Run reopens it (`TemporaryFileManager.reopen`, recreating a removed file), so the 24 h retention counts from the end of the latest Run. All file I/O runs on the process-wide `subagent-activity` `OrderedWorker`: text is handed off at most every 250 ms and at once when a Run ends; beyond 256 pending chunks a chunk of only Assistant text is dropped and a missing-activity note marks the gap (headings, Tool and status lines are always written); a watcher its Run evicts for lagging still writes the Run's outcome. `drain_activity()` waits until the text so far is on disk; Runtime shutdown awaits it after the Run manager closed. Failures affect only observability. Coverage: `tests/core/subagents/test_activity.py`.

## Interfaces

- `SubAgentCoordinator(runtime, trigger_service)`; `install(run_manager)` registers forwarding as a Run-started callback. Bootstrap (`core/runtime/_bootstrap.py`) passes `runtime._chat_run_manager` because the started-service accessors raise before the Runtime has started.
- `spawn(context, arguments)` is the `subagent` handler; `message_parent(context, arguments)` the `message_parent` handler.
- `stop_tree(address) -> int` is "Stop all" (`/stop all`, `chat.stop_all`, WebUI); returns how many Runs, queued items, commands and terminals it stopped.
- `subagent_taken_over(address)` is Chat's takeover hook (`ChatLoopDependencies.subagent_taken_over`).
- `inspect(agent_id, session_id, subagent_id, project_id=None)` returns the WebUI projection of one Sub-Agent Session (active Run, queued, or its latest persisted Run result); `chat_methods.py` serves it.
- `references_identity_agent(agent_id)` guards Identity rename while a followed Run in that Identity's Session is still forwarding.
- `prompt_targets(agent, project_id)` feeds the `tool:subagent` block; `drain_activity()` is for shutdown.
- Events (WebUI, through `ToolContext.emit`): `subagent_session_started` on `run` and `send` (`id`, bare `agent_id`, `session_id`, `project_id` when set, `status` `running`/`queued`, `delivery: "automatic"`, `activity_file`, `run_id` for `run`), `subagent_status_changed` on `cancel`. Events and metadata keep bare ids; Agent-facing results use the qualified address.
- Sub-Agent Runs are admitted with `RunKind.SUBAGENT`, `work_id` = public id, `owner` = the caller's execution owner, and count toward Agent activity only without an owner. The executor is `streaming_chat_loop.run_executor(content, parent_agent_input=True, temporary_parent_binding=...)`, which is steerable.
- Deliveries go through `TriggerService.submit_completion`: forwarded answers with notice id `subagent:<run_id>`, the Run's execution owner, and `on_persisted` marking that terminal Run read; Parent messages with a fresh notice id; the takeover notice `subagent-takeover:<id>` with `wake=False` (persisted without starting a Run; an active Parent Run reads it at its next boundary).

## Conventions

- **`run`** always starts a new Sub-Agent in the background and returns at once with `id`, `agent_id` (the address the Tool accepts back: `agent@project` when the Session has a Project), `session_id`, `project_id`, `status: "running"`, a `note` and an `activity_note`. Checks, all before any Session work, in this order: id/`session_id` interpretation, `description` present, target allowed, target resolvable and runnable, depth, active limit. `run` with an `id` or `session_id` naming one of the caller's own Sub-Agents is treated as `send`; an unknown `sub_`-shaped id fails `subagent_not_found`; another id is an ignored label (note); a `session_id` that is not the caller's Sub-Agent starts a new one when it reads as a stand-in (note) and is refused otherwise. `background: false` is ignored with a note. Each start logs one INFO `Sub-agent started` line (`vbot.subagents`) with ids, Sessions, target and Run.
- **`send`** reaches only a direct child that is not taken over (`subagent_not_direct` names the intermediate Sub-Agent; `subagent_taken_over`); the caller cannot message grandchildren, and Sub-Agents cannot message siblings. It enqueues through `ChatRunManager.enqueue`: an idle Session starts a turn (`started`); otherwise the item is steered into the running turn (`steered`) unless it carries an execution owner or a temporary binding, which stay `queued`. `model`/`thinking_effort` replace the stored overrides; a stored Model that cannot run, or unreadable stored overrides, refuse before enqueueing.
- **`list`** (`status` is accepted) shows the caller's whole tree breadth first: `id`, `title`, `agent_id`, `session_id`, `parent_id` for deeper entries, `state` (`working`, `queued`, `idle`, `taken over by the user`), `last_tool`, `running` (its unfinished background commands and terminals) and `activity_file`. It never repeats answers, which already reached the Parent; a list-level `note` against polling appears while anything works. `id` narrows it to one entry.
- **`cancel`** stops any Sub-Agent below the caller and its whole subtree: queued items removed (`clear_queued`, including input being steered into the Run at that moment), active Run cancelled with reason `parent_agent` and initiator `parent_run:<run_id>`, terminals and background commands closed. A direct child's outcome is silenced (the Tool result reports it); cancelling a deeper Sub-Agent forwards its cancelled answer to its own Parent; everything below the target is silenced. Nothing to stop fails `subagent_not_running`. The Session and its history stay; `send` continues it.
- **Forwarding** follows every Run of kind `subagent` or `system` (`FORWARDED_RUN_KINDS`: turns the Parent started and turns started by deliveries) in a linked, not taken-over Session; user, Channel and other Runs never forward. When the Run ends, the section carries the outcome (completed, cancelled, cancelled by the user, interrupted, failed), the final answer (the Run's message, else its persisted answer, read up to 3 times 50 ms apart) and the facts line: unfinished terminals and background commands attached to that Session plus its working, not taken-over direct children, as of the forward. When the Parent has other, not taken-over Sub-Agents, a second line names those whose answers are still to come (working, or a followed, not silenced Run that has not yet handed its answer to delivery), or says that none are. It is dropped when the Run was silenced, the Session was taken over meanwhile, or the Parent Session no longer exists (INFO log).
- **Parent framing:** Parent input reaches the Sub-Agent with `PARENT_AGENT_INPUT_SYSTEM_REMINDER` at a turn start or `PARENT_AGENT_STEERING_SYSTEM_REMINDER` when steered (`core/chat/messages.py`). The `core:subagent_role` System Prompt block (owner `subagent_session`, text `resources/prompts/subagent_role.md`) and the session-scoped `message_parent` grant (`SUBAGENT_SESSION_TOOL_NAMES`) apply exactly when the Session has a Parent link; Chat reads that flag per Run context and at Compaction, and it does not change on takeover.
- **Takeover** is detected by Chat when it persists the first input in a linked Session that is neither Parent input nor internal, on both the Run-start and the steering path (`takes_over_subagent_session` / `record_subagent_takeover` in `core/chat/_run_state.py`): `SUBAGENT_TAKEN_OVER_SYSTEM_REMINDER` precedes the message, the timestamp is recorded after persistence, and the hook sends the Parent the non-waking takeover notice once. Afterwards: no forwarding, `send` and `message_parent` refuse, `list` shows the state.
- **`message_parent`** delivers a section to the Parent Session (waking it) without ending the Sub-Agent's turn; refusals `not_a_subagent`, `subagent_taken_over`, `parent_not_found`.
- **Stop (every Session and Channel):** Run cancellation stops only the current Run and its foreground commands (`TerminalManager.cancel_run` closes hidden Terminal Sessions only); background commands, terminals and Sub-Agents keep working and deliver later, waking the Session. `stop_tree` stops the Session's Run (its own Queue stays, its answer is not silenced) and, for every Sub-Agent below, clears the Queue, cancels and silences the Run, and closes terminals; reason `user`, initiator `user_stop_all`.
- **Limits** (Sub-Agent settings, `settings.md`): `max_subagent_depth` (default 4) refuses a `run` when the caller already has that many ancestors; `max_active_subagents` (default 8) refuses when the working Sessions of the tree reach it; starts of one tree are serialized for this check. `send` to an idle Sub-Agent starts its next Run without this check.
- **Targets:** a bare `agent_id` inherits the caller's scope; `agent@project` selects that Project. The Sub-Agent Session lives in the target scope, its link records the Parent address, and the Parent Session never moves. `tools.subagent.allowed_agents` (snapshotted in the caller's Tool settings) lists additional targets only: missing or `['*']` allows every resolvable target, `[]` is self-only, omitting `agent_id` always selects the caller. A Project caller can reach only its own Project, whatever the policy. A generic worker name (`general-purpose`, `default`, `self`, ...) that is no allowed Agent id selects the caller with a note. The built-in Librarian is never a target. `agent_not_found`/`project_not_found` list the valid choices; a target that cannot run fails `agent_unavailable` (`retryable: false`) with the resolver's reason; an unusable `model` fails `invalid_arguments`. The coordinator is the security boundary; Provider-schema narrowing and the prompt block are guidance.
- **Execution ownership** (temporary Agents, extension groups): Sub-Agent Runs and queued sends carry the caller's owner, forwarded sections carry the Run's owner, and a copy of a temporary caller runs under its temporary parent binding without the private Session grants. A closed owner drops its deliveries silently (`RunAdmissionBlockedError`, DEBUG). `tests/core/agents/test_temporary.py` covers actual Chat execution.

## Constraints & Gotchas

- Forwarding and activity state are process-local: a Run is followed only when it starts in this process (Runs do not survive restarts either), and an activity file allocated before a restart is not reused.
- After a plain Stop, a Session's Sub-Agents and background work continue, and their later answers wake it. That is intended; Stop all is the control that ends them.
- The domain's Agent-facing texts live in `_constants.py` (results, refusals, forwarded sections), `resources/prompts/subagent_role.md` (role block) and `core/chat/messages.py` (reminders). A new prompt fragment must also be listed in `PROMPT_FRAGMENT_NAMES` (`core/storage/prompt_fragments.py`), or Runtime start fails.
- Tests: `tests/core/subagents/subagents_test_support.py` dispatches through the registered Tools over real Sessions and a real `ChatRunManager`; only the Agent resolver, the completion coordinator and the Chat loop are doubles. Coverage: `tests/core/subagents/test_subagents.py`, `tests/core/tools/test_subagent.py`.

## References

- Changing the `subagent` or `message_parent` definitions, call syntax, display or the `tool:subagent` block -> `tools/subagent.md`
- Changing how deliveries coalesce, wake or persist -> `automation.md`

## Agent-facing text

| Text | Reason |
|---|---|
| Role block: `You are a Sub-Agent: another Agent, your Parent Agent, delegated work to you ...` | Without it a Sub-Agent takes the brief as the user's request and addresses the user. |
| Role block: `vBot sends the final answer of each of your turns to your Parent Agent. ...` | Every final answer is forwarded; a Sub-Agent that ends a turn while work still runs must say so instead of presenting a result. |
| Role block: `vBot starts a new turn for you when background work you started delivers ...` | Sub-Agents otherwise poll or wait on background work they can leave running. |
| Role block: `If the user writes in this Session, vBot stops sending your answers ...` | Takeover changes who reads the answers; the block stays pinned after it. |
| `PARENT_AGENT_INPUT_SYSTEM_REMINDER` | Marks Parent messages as not from the user. |
| `PARENT_AGENT_STEERING_SYSTEM_REMINDER` | A steered Parent message must change the running task, and a question in it needs `message_parent` because the final answer comes later. |
| `SUBAGENT_TAKEN_OVER_SYSTEM_REMINDER` | Tells the Sub-Agent at the moment of takeover that its answers now go to the user. |
| Forwarded section header and facts line | The Parent must tell which Sub-Agent answered, how its turn ended, and what is still running before it treats the answer as final. |
| `FACTS_SIBLINGS_PENDING_TEMPLATE` / `FACTS_NO_SIBLINGS_PENDING_TEXT` | After one of several Sub-Agents answered, a Parent called `list` to learn whether the others still ran (black-box runs, 2026-10). |
| `TAKEN_OVER_NOTICE_TEMPLATE` | The Parent would otherwise keep waiting for answers that no longer come. |
| `PARENT_MESSAGE_SECTION_TEMPLATE` closing sentence | A message is not the Sub-Agent's result; the Parent must keep waiting for the answer. |
| `MESSAGE_PARENT_SENT_NOTE` | Prevents a Sub-Agent from ending its turn after a message in the expectation of a reply within the same turn. |
