# Interactive Terminal Sessions (`terminal_beta`)

## Boundary and invariants

`terminal_beta` gives an Agent a real interactive terminal rather than Bash's pipe-based Process Session. Every requested executable uses the same PTY/ConPTY transport and receives the exact declared argument vector. The terminal capability contains no executable detection, program-specific flags, hooks, configuration injection, lifecycle protocol, or screen-text heuristics. Codex, Claude Code, other coding CLIs, shells, editors, games, and arbitrary interactive programs therefore share one contract.

- A Terminal Session belongs to one exact `TerminalOwner(project_id, agent_id, session_id)` and survives individual Runs. Agent operations enforce that owner; the local operator projection may observe and control every active Terminal Session without changing ownership.
- The Agent Tool, WebUI, and child process use the same live PTY/ConPTY. Agent and WebUI input are written to that process, WebUI output is the sequenced raw VT stream, and Agent status is rendered from the same stream.
- A Terminal Session ends only on explicit `kill`, natural child exit, owner Session/Agent/Project removal, Runtime shutdown, or unrecoverable terminal failure. Leaving a Run or navigating away from the WebUI does not end it.
- Live states are `starting`, `ready`, and `working`; terminal states are `exited` and `error`. These states describe terminal activity and process lifetime, not application-specific turns or prompts.
- Raw output and rendered history are bounded. When Storage is available, the manager additionally retains the raw output in a leased `<data_dir>/artifacts/temp/terminals/*.log` and starts the normal temporary-file retention clock after the Terminal Session ends.

## Agent Tool contract

`core/tools/terminal_beta.py` exposes one open flat action schema and performs action-specific accepted-field and semantic validation. `start` defaults to command `codex`, no arguments, the current working directory, 120 columns, and 32 rows; this is only a convenience default and does not select a different launch path.

- `start`: launches exact `[command, *args]`; optional `text` waits for a stable initial TUI screen and then sends text followed by Enter. The result explains that the Terminal Session survives the Run and that output settling is not a semantic completion signal.
- `list`: returns compact retained Terminal Session summaries for the current owner.
- `status`: returns the current rendered screen, up to 30 prior scrollback lines by default, at most 100 lines, an opaque signed cursor for older pages, process facts, dimensions, screen/activity revisions, and the raw-log path when available. A cursor continuation accepts only `action`, `terminal_id`, and `cursor`.
- `wait`: waits at most 1,000 ms by default and 10,000 ms maximum for a newer generic activity revision, exit, or error. A timeout is a successful current snapshot with `timed_out=true`; it never owns or pauses the child.
- `input`: accepts either exact non-empty `data`, written unchanged in one PTY write, or the convenience combination of non-empty `text`, one named `key`, and `enter`. `data` cannot be combined with the convenience fields. Named keys cover navigation, Insert/Delete/Home/End/Page Up/Page Down, Shift-Tab, F1-F12, and Ctrl-A through Ctrl-Z; arbitrary sequences use `data`. Optional `expected_screen_revision` rejects stale input.
- `resize`: validates conservative bounds, resizes the host PTY/ConPTY and renderer together, and returns the new dimensions and screen revision.
- `kill`: terminates the complete child process tree, suppresses an automatic exit delivery caused by that explicit action, and leaves a bounded exited snapshot until finished-session expiry.

## Generic activity delivery

Generic terminal bytes cannot reliably distinguish a question, approval request, completed application-level task, idle animation, or prompt. `TerminalManager` therefore emits only `output_settled`, `exited`, and `error` attention kinds and never claims application semantics.

- Agent input starts one activity cycle, changes state to `working`, records the current Run as delivery origin, and arms a two-second quiet timer. Every later PTY output restarts that timer. When the stream stays quiet, state becomes `ready`, a monotonically revisioned `output_settled` record is created, and `TriggerService.submit_completion` wakes the owning vBot Session. The notification explicitly requires the Agent to inspect `status` and says that quiet output does not imply completion or a need for input.
- Operator input and spontaneous output use the same state and quiet detection, but do not independently request an automatic Agent wakeup. Their settled revision remains observable through `wait`.
- Natural exit and terminal failure create `exited` or `error` attention and request delivery. Explicit kill and scope shutdown suppress that delivery.
- `status`, `wait`, `input`, and `kill` acknowledge an equivalent pending delivery only through `ToolContext.after_result_persisted`; handler return alone is not durable acknowledgement. Scope transfer reroutes a pending delivery to the new owner.

## Transport, rendering, and local operator surface

`core/tools/terminal_manager.py` is the deep owner of Terminal Session identity, authorization, lifecycle, activity timing, attention delivery, retained output, stream sequencing, and cleanup. `core/tools/terminal_backend.py` hides the blocking platform adapter, VT renderer, alternate-screen handling, Windows launcher adaptation, and process-tree termination. Blocking adapter operations run through `asyncio.to_thread`.

- Windows uses `pywinpty`/ConPTY and POSIX uses `ptyprocess`/PTY. On Windows, resolved `.cmd` or `.bat` launchers use the platform command processor as a generic executable-format adapter; no command name receives special handling.
- `pyte` owns bounded terminal rendering. The renderer preserves a separate primary and alternate screen, resizes both buffers, restores the primary screen on alternate-screen exit, and serializes the active screen into ANSI for late WebUI viewers.
- The manager publishes an authoritative ANSI snapshot followed by contiguous sequenced output/state events. `server/rpc/terminal_methods.py` maps operator list/input/resize/kill and `/ws/terminals/{terminal_id}` carries the selected stream.
- `webui/src/lib/terminalsView.js` owns active-catalog reconciliation, stream reconnect and gap recovery, exact input batching, resize debounce, and teardown. `TerminalsView.svelte` dynamically mounts xterm.js, defaults to observe mode, enables direct keyboard and paste only after explicit operator control, offers deliberate text-plus-Enter input, and confirms process-tree stop. Navigation and socket teardown never own the Terminal Session lifetime.

## Runtime and lifecycle integration

Runtime constructs `TerminalManager` after `TriggerService`, registers `terminal_beta` before System Prompt assembly, exposes it through `RuntimeServices`, and closes it before temporary-file cleanup. Run cancellation never closes Terminal Sessions. Session deletion, Agent removal, Project removal, and successful `/agent` moves close or transfer exact Terminal owner scopes inside their existing lifecycle boundaries. `terminal_beta` is an ordinary built-in Tool and is part of the default Project Tool allowlist.

Focused coverage lives in `tests/core/tools/test_terminal_backend.py`, `test_terminal_manager.py`, and `test_terminal_beta.py`, with Runtime, lifecycle, RPC, WebSocket, controller, and rendered-component coverage in their owning test trees. Real Windows validation must use actual ConPTY programs and an unmodified installed Codex CLI because fake adapters cannot prove executable launch fidelity, alternate-screen rendering, browser keyboard/paste behavior, resize, or process-tree stop.
