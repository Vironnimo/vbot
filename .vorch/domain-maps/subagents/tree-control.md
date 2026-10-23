# Sub-Agent Tree Control

How Sub-Agents are listed, checked, inspected and stopped: `background_tasks` list, status and stop, Stop and Stop all, the `chat.work` entry list, `inspect`, and the tree projections and events the WebUI reads.

## `background_tasks` list, status and stop

List shows every Sub-Agent in tree order, then commands of the caller and every descendant; a descendant's command names it in `subagent`. It never repeats answers. A note against polling appears for the caller's running commands or direct working/queued children whose reply target is Parent. Status adds `project_id`, non-empty `running` and actionable `next` text. A user-directed child keeps its ordinary status and adds `reply_to: "user"`; its next text names sending a message to restore Parent replies (only for a direct child with `subagent` available), and stopping it. A `ses_` id identifying exactly one descendant is read as that Sub-Agent with a note. Coverage: `test_background_tasks_lists_and_checks_the_whole_tree`, `test_user_input_keeps_the_complete_subagent_tree_controllable`, and `tests/core/tools/test_background_tasks.py`.

Stop (`stop_subagent`) stops any descendant with its whole subtree: queued items removed (including input being steered), active Runs cancelled with reason `parent_agent` and initiator `parent_run:<run_id>`, terminals and background commands closed. A direct child's outcome is silenced because the Tool reports it; a deeper child's cancellation can forward to its own Parent if addressed there. Descendants of the stopped target are silenced. The result is success with `status: "stopped"` or `"idle"` when nothing needed stopping. Sessions and history remain; the result names continuing with `send` while the caller can call `subagent`, or names the intermediate Parent for a deeper child. User input changes none of this.

## Stop and Stop all

Run cancellation stops only the current Run and the shell commands still inside its calls (`CommandManager.cancel_run`, `tools/shell.md`); background commands, terminals and Sub-Agents keep working and deliver later, waking the Session. `stop_tree` stops the Session's Run (its own Queue stays, its answer is not silenced) and, for every Sub-Agent below it, clears the Queue, cancels and silences the Run; it closes the terminals and background commands of the Session and of every Session below it (`commands.close_scope`, without delivery; `test_a_tree_stop_stops_its_background_commands_without_delivery`); reason `user`, initiator `user_stop_all`. Terminal cleanup closes the captured address set in one batch, so an admitted launch without a published Terminal row is drained and a Terminal attached within the same tree is counted only once. The entire captured subtree cleanup settles before propagating caller cancellation; a resource failure is reported only after the other resources and Runs were given their cleanup (`test_stop_all_drains_terminals_of_the_complete_tree`). Both tree stops take the admission lock while signalling (`subagents/delegation.md` -> Cancellation admission).

## Live work entries

`chat.work` requests `include_entries=True` to add a neutral `running` list from the same strict descendant snapshot, independent of loaded conversation History. Each Sub-Agent, command and Terminal entry carries its exact Agent/Project/Session address and identity; Sub-Agents include parent/reply-target/active-Run facts, commands include `still_running`. Existing boolean-only callers keep the cheaper default projection.

## inspect

`inspect(agent_id, session_id, subagent_id, project_id=None)` returns the WebUI projection of one Sub-Agent Session (active Run, queued, or its latest persisted Run result) plus `running`, what it still runs besides its own Run, from `running_entries`: `{kind, id, label}` with kind `subagent` (working; label its title), `command` or `terminal` (label its name or command line); `chat_methods.py` serves it.

## Tree projections and events

- `subagents(caller)` returns frozen `SubAgentState` rows: `id`, `title`, `agent_id`, Session, `parent_id` (`None` for a direct child), `status` `working` | `queued` | `idle`, `last_tool`, `activity_file`, and independent `reply_target`; `answer_pending` says that a direct child works and its answer will reach the caller. `running_work(subagent)` lists commands, terminals and working children as `kind id (label)`.
- `subagent.tree {agent_id, session_id}` (served by `subagents`) returns the Session's Sub-Agents at every depth breadth first with `id`, `title`, qualified `agent_id`, `session_id`, `parent_id` (null for a direct Sub-Agent) and `status` `working|queued|idle`, for the Session information panel.
- `subagent_session_started` (on `run` and `send`) carries `id`, bare `agent_id`, `session_id`, `project_id` when set, `status` `running`/`queued`, `delivery: "automatic"`, `activity_file`, and `run_id` for `run`; `subagent_status_changed` carries status `cancelled`.
