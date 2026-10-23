# Context Commands

Read when changing the commands a Session starts with: which Runs run them, where, how their outputs reach the Agent, and the configuration fields on Projects and Identity Agents. User decision 2026-10-23: the user stores shell lines in vBot's settings (never in repository files) so that an Agent gets data such as `git status` with its first message instead of fetching it in its first turns.

## Configuration

- `context_commands` is an optional list of shell lines on a Project (`project.json`) and on an Identity Agent (`agent.json`): at most 10 single-line commands of at most 4,000 characters, stripped, each once in order (the WebUI edits one per line), written only when non-empty (additive to Generation 1). The shape is validated in one place, `core/utils/context_commands.py`, and reported as a diagnostic `$.context_commands` by both document validators.
- Set through RPC `project.set` and `agent.update` (`agent.create` takes none), the CLI `project set --context-commands <command>...` and `agent update --context-commands <command>...` (no values clears), and the WebUI Project and Agent settings (one command per line). Project and Agent payloads always carry the list.
- Built-in Agents have none. Project Agents have no own list: a Session in a Project runs the Project's.

## When they run

`core/chat/_context_commands.py` decides and formats; `core/chat/_run_execution.py` (`_start_context_commands`, `_persist_context_commands`) wires them into a Run.

- Only until the note exists: the Run's prior history has no assistant message and the Session metadata has no `context_commands_ran` marker. A first Run stopped before its note therefore leaves the commands to the next Run, whose note follows its own input. Forks carry their source's answers, so they never run them.
- Run kinds `user`, `channel`, `cron`, `calendar`, `subagent` and `system`; never reflection, Librarian or other internal Runs, Runs of temporary bindings (Extension Sessions), owned Runs (`execution_owner`), message edits, or Runs whose input was persisted before.
- The commands of the Run's Working Project when it has one (run in the Project folder; the Agent's own list is ignored), else the Identity Agent's (run in its Workspace).
- All commands start in parallel while the Run persists its input; the Run awaits them before its first Model request. A Stop or failure ends the commands still running (their processes end with them) and saves no note. A failure to plan or persist logs WARNING and the Run goes on without the note.

## Runner

`WatchCheckRunner.run_context_command` (`core/tools/watch_checks.py`, the Cron check runner; `automation/cron-watch.md` -> Check runner): the host shell, 60 s timeout, 64 KB output, the environment of a check. Unlike a check, a non-zero exit is returned with its output, because it is the command's answer (`git diff --exit-code`). Under a restricted permission boundary the command is checked with the Session's boundary and runs without the credentials vBot knows, as the shell Tool would run it; a refusal or a missing working directory becomes the entry's `error`. Chat reaches the runner through `ChatLoopDependencies.get_context_command_runner`, bound late because Runtime creates the runner after the Chat Loop.

## The note

One persisted note (`ChatMessage.note`, a System Reminder; `model-communication.md`) after the first input, before the first Model request, recorded in the metadata marker as `{"note_id": ...}`. It is a snapshot: nothing refreshes it, and Compaction summarizes it like other history.

- A sentence names who set the commands up (`for new Sessions in this Project` or `for your new Sessions in your Workspace`) and the working directory, then a JSON record per command in order: `{"command", "exit_code", "output", "cut"?}` or `{"command", "error"}`, escaped with `quote_external_value` so angle brackets in outputs cannot open tags. Commands, outputs and errors are masked for known Secret values.
- Outputs are LF-normalized, trimmed of trailing spaces and surrounding blank lines, and cut to an equal share of 8,000 characters (`CONTEXT_OUTPUT_MAX_CHARS`) among the commands that ran, at a line break in the second half of the share where one exists; a cut is named in the note.
- Wording and reasons: `chat/agent-facing-text.md`.

## Coverage

`tests/core/chat/test_chat_loop_context_commands.py` (first Run only, position, marker, request, escaping and cuts, errors, forks, a Stop before the note, Project scope), `tests/core/tools/test_watch_checks.py` (exit codes, refusal, missing directory), the Project and Agent store, validation, RPC and CLI tests, WebUI `agentForm.test.js` and `projectsView.test.js`.
