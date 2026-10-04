# Shell Tool (`bash` / `powershell`)

Runs one host shell command per call, each in a new Terminal Session of kind `command`, and returns its rendered output with an honest outcome.

`core/tools/shell.py` owns the Tool: definition and depth projection, call parsing, the wait/hand-off decision, result and delivery text, the durable status fold, and display. The terminal domain (`tools/terminal.md`) owns everything below it: the PTY/ConPTY, the command session (`_terminal_command.py`: transcript, `CommandReport`), process-tree tracking (`_terminal_process_tree.py`), and the command API of `TerminalManager`. `core/tools/shell_environment.py` builds the environment of every shell vBot starts, commands and interactive terminals alike. `_shell_arguments.py` owns other harnesses' argument shapes, the `env` object, timeout units and display parts. `update_handoff.py` owns the `vbot update` token (`cli/application.md`).

A command that outlives its call is a listed Terminal Session, followed up through `terminal` (`wait`, `input`, `kill`) and delivered by the terminal attention path; add follow-up capabilities there, not as a second process-tracking layer.

## Terms

### Command terminal
A Terminal Session started by `spawn_command` for one shell call. It is hidden (absent from the Terminal list, catalog and `resource_changed` events) until handed off.

### Hand-off
`hand_off_command(terminal_id, deliver=...)` makes a still-running command terminal a listed Terminal Session. With `deliver`, the command's result is submitted to its Session as a completion when the shell exits. Hand-offs happen on `mode: "background"`, after the 90-second wait at depth 0, when the command looks idle (waiting for input), and when the user moves the call to the background.

## Interfaces

- Registry name `bash` (`SHELL_TOOL_NAME`); the Model sees `powershell` on Windows and `bash` elsewhere (`SHELL_MODEL_NAME`, `tools.md` -> Model Tool names). Registration: `register_shell_tool(registry, terminal_manager, credential_resolver=..., prompt_blocks=..., update_handoffs=...)`; it also declares the dynamic `tool:bash` Prompt Block `## Shell Environment Access` (empty without permanent Agent env grants; text from `format_shell_env_usage`, shared with Skill env guidance).
- Schema: one open flat object requiring `command`, with optional `description`, `workdir`, `timeout`, `env_keys`, and at depth 0 `mode` (`foreground` | `background`). `project_shell_tool_definitions(definitions, nesting_depth=...)` (called by Chat's request builder) fits the description to the depth and to the Tools actually offered (the file-Tool pointer names only offered `read`/`search_files`/`apply_patch`; the continuation sentence depends on `terminal` being offered) and drops `mode` at depth >= 1. The handler independently refuses `background` at depth >= 1 (`background_unavailable_in_subagent`).
- Unadvertised aliases and dialects (`_shell_arguments.py:normalize_shell_arguments`): `cmd`/`script`/`commands`, argv arrays (joined into one host command line; a host-shell `-c`/`-Command` wrapper is unwrapped), OpenAI `local_shell` `action` objects, `cwd`/`directory`/`working_directory`, `timeout_ms`/`timeout_seconds`, background booleans, title-like description fields, harmless foreign flags (`pty`, `tty`, `login`, ...). Calls asking for a different effect (`elevated`, escalated sandbox permissions, `require_user_approval: true`, a non-empty `user`, contradicting mode fields) fail before running with the fix.
- `timeout` is seconds of total runtime including time after a hand-off; omitted -> 600 (`SHELL_DEFAULT_TIMEOUT_SECONDS`), `0` -> no limit. A value that is clearly milliseconds is read as such with a `notes` entry; disagreeing `timeout`/`timeout_ms` fail.
- `env` (unadvertised) sets plain variables; vBot never passes a credential value written in the call. Credentials come only through granted `env_keys` names resolved server-side. An ungranted `env_keys` name the server process itself has passes with a `notes` entry; an unknown one fails, naming the granted keys.
- Host shell: Windows `pwsh -NoProfile -Command <script>` with a `[Console]::OutputEncoding/InputEncoding = UTF-8` prefix (skipped when the script starts with `using`/`param`/`#requires`/`[CmdletBinding`, which must lead); an appended exit statement passes the last native program's exit code on (`pwsh -Command` alone reduces every failure to 1; skipped for a script made of `begin`/`process`/`end` blocks, which accepts no trailing statement); command lines over 32,000 characters fail with a save-to-`.ps1` fix. POSIX `bash -c <command>`. A missing `pwsh`/`bash` fails with "ask the user to install it". The command terminal is 200x50.
- Results (`tool_success` data; `ok: true` even for non-zero exits):
  - exited: `status: "exited"`, `exit_code`, `output`, plus when present `log_file` (only when output was cut), `failed_programs` (direct child programs of the shell that exited non-zero, e.g. `git.exe exited with code 128`; when vBot stopped the command, only those that failed before the stop), and for processes the command left running `terminal_id`, `still_running` (`name (pid N)`) and `next` (kill them with `terminal kill`).
  - stopped by vBot: `status: "stopped"`, `stopped_because` (timeout with the retry fix, the user, the cancelled Run, shutdown, or "you stopped it"), and the fields above.
  - still running: `status: "running"`, `terminal_id`, `output` (transcript plus the newest 40 screen rows), optional `log_file`, and `next`: the hand-off hint, or the idle hint when the command printed nothing for 15 seconds and its whole process tree used under 0.15 s CPU. At depth 0 both say the result arrives as a new message.
  - `notes` lists how the call was read leniently (millisecond timeout, inherited env key).
- Output shaping (`command_output_text`): the transcript is the rendered output as final logical lines (no ANSI, no progress-bar redraws), held as the first 40 and last 80 lines; the result keeps whole lines within 4,000 head and 8,000 tail characters and cuts single lines at 2,000 characters, with an in-text marker naming `log_file`. The full transcript is a private file in Storage's `artifacts/temp/commands` (72 h retention).
- Delivery (`format_command_delivery`): a handed-off command's result reaches its Session as a completion Note starting `The command in terminal <id> (<description or first line>) exited with code N.` or `... was stopped: <reason>`, then failed programs, still-running processes, `Full output: <log>` when cut, and the output. It goes through `TriggerService.submit_completion` with the origin Run id, so it survives a restart like every completion. An Agent's own `terminal kill` delivers nothing.
- Status fold (`background_command_statuses(records)`): durable `bash` results with `data.status == "running"` and a `terminal_id` give `running`; delivery Notes give `completed` (exit 0), `failed` (other code) or `stopped`. Folding a later range onto an earlier fold equals folding both. `chat.history` returns it as `background_command_statuses` after replacing each `running` with `TerminalManager.command_status(id)` and dropping commands vBot no longer knows. Live changes are the `command_status_changed {terminal_id, status}` app event (`server/events-and-reconnect.md`).
- Live output: while the call waits, the Tool emits the Run event `tool_call_output {tool_call_id, terminal_id, screen}` at most every 0.5 s while output arrives; `screen` is the newest 40 rows and replaces the previous one (transient, never persisted).
- Display: `shell_display_parts` shows the `description` or the command (copyable either way); `shell_detail_blocks` shows the command, the result's `data.output`, and warnings for a stop, a non-zero exit code and failed programs, or an info notice for a running command.

## Execution lifecycle

1. `_parse_call` validates and resolves everything before anything starts: `workdir` (relative to `effective_cwd`; a missing directory fails naming similar directories), timeout, env, credentials.
2. An update handoff token is issued when the call has a result-persistence boundary; it stays claimable exactly until the command's whole process tree has ended (`wait_finished`).
3. `command_environment` builds the environment (below); `spawn_command` starts the shell in a hidden command terminal. A process tree that cannot be tracked is not started. A cancelled Run's id is rejected (`cancel_run`).
4. Foreground: `wait_command` waits for exit, the hand-off deadline (90 s at depth 0, none deeper) or idleness (only when the Agent can call `terminal`). At depth 0 the user can move the call to the background (`background_registration_hook`). Per-call cancellation stops the command (`run_cancelled` or `user`); Run cancellation also terminates the Run's handed-off commands, without delivery (`TerminalManager.cancel_run`).
5. Exit -> exited/stopped result; a shell that exited while processes it started still run is handed off without delivery, so the user and Agent can see and stop them. Otherwise the command is handed off (delivering at depth 0) and a running result returned. Background mode hands off at once.

`stop_command(reason)` sends Ctrl+C, waits 3 s for the shell, then kills the whole tree. The timeout keeps running after a hand-off. `shutdown_commands` stops every command before the delivery service closes, so handed-off commands deliver a "vBot shut down" result.

## Environment layers (`shell_environment.py`)

Built fresh for every start (about a millisecond; no cache), each layer over the previous:
1. Login base: Windows a fresh user environment block (registry `PATH` included, so newly installed programs are found); POSIX the server environment. vBot's own process variables (`PYTHONHOME`, `PYTHONPATH`, `VIRTUAL_ENV`, `__PYVENV_LAUNCHER__`, the venv on `PATH`) are removed.
2. Instance: `VBOT_DATA_DIR`, `VBOT_SERVER_PORT`, `VBOT_INSTALL_ROOT` exactly when the server has them.
3. `TERM=xterm-256color`.
4. Commands only - unattended defaults: `GIT_EDITOR`/`GIT_SEQUENCE_EDITOR` print a pass-it-as-argument message and fail, `GIT_MERGE_AUTOEDIT=no`, `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `GIT_PAGER`/`PAGER=cat`.
5. Commands only - `env` variables, then granted credentials.
6. Commands only - Run identity last: `VBOT_RUN_AGENT_ID`, `VBOT_RUN_SESSION_ID`, `VBOT_RUN_PROJECT_ID`, `VBOT_UPDATE_HANDOFF` (an inherited token is always removed).

Interactive Terminal Sessions use `terminal_environment()` (layers 1-3).

## Constraints & Gotchas

- Process escape: Windows tracks the tree with a Job Object (children that break away, as the update handoff does, leave it; a process created between the root's start and job assignment escapes). POSIX tracks the shell's session; a process that calls `setsid` leaves it. Escaped processes are neither reported nor killed.
- Child exit codes come from Job notifications on Windows, which are best effort: a missing `failed_programs` entry proves nothing. POSIX reports no child exit codes.
- Env grants are not a sandbox: a command can print or send an injected credential. The shell runs with the server user's full rights.
- POSIX commands get the server's environment, not a login shell's `PATH`.
- Starting a command costs about 0.45-0.8 s (PTY, shell start, tree tracking) before it runs.
- Capacity: at most 64 live command terminals, independent of the interactive limits (4 per Session, 32 global).
- A Sub-Agent never hands off with delivery: its Session cannot receive a completion after its Run ends. At depth >= 1 foreground waits until exit, idleness or timeout.
- Old stored results with `process_id`/`delivery: "automatic"` are plain rows; nothing folds them.

## Tests

`tests/core/tools/test_shell.py` (Tool contract with a fake clock and a tracked executor, `terminal_manager_helpers.py`), `test_shell_arguments.py` (dialects, timeouts, env, display), `test_shell_environment.py`, `test_terminal_commands.py`/`test_terminal_process_tree.py` (terminal domain), `test_update_handoff.py`, `tests/server/rpc/test_chat_methods_history.py` (status projection), `tests/server/test_app_events.py` (status event dedup).

## Agent-facing text

| Text | Reason |
|---|---|
| Description: `Run a PowerShell 7 command in a new terminal and return its output and exit code.` (`bash` elsewhere) | Names the real shell and edition, so Models write the right syntax (F1, F3); "new terminal" says no state carries over between calls. |
| Description: `Use PowerShell syntax.` | Models default to bash syntax under any shell name (F3). |
| Description: `Output is rendered terminal text; use read, search_files and apply_patch to read, search and edit files.` | Output is what a person would see, not a byte stream (F4); dedicated file Tools beat shell file handling (F1). Lists only offered Tools. |
| Description: `No editor or credential prompt is available: pass commit messages and answers as arguments.` | Editor and credential prompts were the main hang cause; the unattended environment makes them fail at once, this sentence prevents the failed call (F3). |
| Description: `A command still running after 90 seconds, or waiting for input, continues as a terminal; the result says how to proceed.` | Agents must expect a `running` result instead of rerunning (F4). Depth and offered-Tool variants drop the parts that do not apply. |
| `timeout`: `Seconds before the command is stopped. Omit for 600; 0 for no limit.` | Unit and default (F2, F3); JavaScript habits send milliseconds, which the handler also detects. |
| `workdir`: `Directory to run in, absolute or relative to the working directory. Omit to use the working directory.` | Accepted forms and the omit rule, so Agents do not `cd` inside the command or pass `"."` (F2). |
| `mode`: `foreground returns when the command exits, or hands it to the background after 90 seconds.` | States the hand-off up front, so Agents do not pick `background` merely to avoid blocking. |
| `mode`: `background returns at once; use it for servers, watchers and other commands whose result your next step does not need.` | 25 of 56 old `process` waits followed a `background` start of a build or test within seconds (F6; Sessions, 2026-09-26). |
| `mode`: `Background results arrive automatically.` / `Omit for foreground.` | Prevents polling plans (F4); omit rule (F2). |
| Hand-off `next`: `The command continues in terminal <id>. Its result arrives as a new message when it exits. Do not poll or start it again; continue other work or end your turn.` | Agents polled or reran handed-off commands (F4, F6). |
| Idle `next`: `The command printed nothing for 15 seconds and uses no CPU; it is probably waiting for input. Answer with terminal input, or stop it with terminal kill.` | A command waiting for input used to hang until its timeout; this names both valid next actions (F5). |
