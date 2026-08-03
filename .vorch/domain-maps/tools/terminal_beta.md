# Interactive Terminal Tool

Lets an Agent start and control a real TUI in a Session-scoped Terminal Session while vBot owns process lifetime, bounded output projection, and Agent-attention delivery.

## Boundary and data model

- Tool name: `terminal_beta`. Its provisional name intentionally exposes the capability as beta while the contract is exercised in normal Agent use.
- `TerminalManager` owns in-memory `TerminalSession` records and the child process tree. Authority is the exact `(project_id, agent_id, session_id)` `TerminalOwner`; a missing id and a cross-Session id have identical not-found behavior.
- A Terminal Session survives the Run that created or last wrote to it. It remains reusable after a Codex turn completes and ends only on explicit `kill`, child exit, owning Session/Agent/Project removal, Runtime shutdown, unrecoverable transport/render failure, or finished-record TTL eviction after it has already ended.
- Live states are `starting`, `ready`, `working`, `needs_input`, and `turn_complete`; terminal states are `exited` and `error`. `turn_complete` does not imply process exit.
- Attention records are monotonically revisioned and classify `approval`, `question`, `turn_complete`, `exited`, or `error`. A manual `status`, `wait`, `input`, or `kill` result acknowledges equivalent pending delivery only through `ToolContext.after_result_persisted`, never merely because the handler returned.

## Agent-facing contract

- The model-facing schema is one open flat object with required `action`: `start`, `list`, `status`, `wait`, `input`, `resize`, or `kill`. The handler owns action-specific required/inapplicable fields and defaults; unknown fields fail explicitly.
- `start` defaults to command `codex`, no arguments, the current working directory, 120 columns, and 32 rows. Optional `text` is submitted as the first interactive task after the TUI has produced a stable initial screen; the start result explicitly says the Terminal Session continues independently and that automatic attention replaces polling.
- `list` returns compact summaries only for Terminal Sessions owned by the current vBot Session.
- `status` returns the current rendered screen plus at most 30 prior scrollback lines by default (100 maximum). Older scrollback uses a signed process-local cursor; continuation calls send only `action`, `terminal_id`, and `cursor`. Output remains server-side except for the requested bounded projection.
- `wait` is only a bounded same-Run convenience (1,000 ms default, 10,000 ms maximum). A timeout never stops or detaches the child and must not replace automatic attention.
- `input` may send text and named keys; text without a key appends Enter by default. Text and control keys are separate PTY writes with a short delay so paste-sensitive TUIs treat Enter as submission. `expected_screen_revision` rejects stale prompt answers before they can land in a changed UI.
- `resize` updates both the host PTY/ConPTY and VT renderer. `kill` explicitly terminates the process tree and suppresses the redundant automatic exit notice.

## Transport and rendering

- `core/tools/terminal_backend.py` owns the blocking platform adapters, child-tree termination, VT renderer, Windows command-launch adaptation, and Codex launch integration. `TerminalManager` calls every blocking adapter operation through `asyncio.to_thread` and owns Session/attention orchestration.
- Windows uses `pywinpty`/ConPTY; POSIX uses `ptyprocess`; `pyte` renders the VT stream into a bounded current screen and 2,000-line scrollback. This is deliberately separate from `bash`/`process`, whose stdin pipe and ANSI-stripped output remain non-interactive.
- Commands are argv tokens and never shell-interpolated. Windows `.cmd` launchers use a controlled `cmd.exe` fallback; the npm Codex launcher resolves to its adjacent Node entry point so inline hook TOML survives Windows quoting.
- When Storage is injected, raw terminal output is flushed to a leased `<data_dir>/artifacts/temp/terminals/*.log`; Codex hook events use a separate leased JSONL file. Both start the 72-hour Storage retention clock after the Terminal Session ends. The Agent receives only the log path plus bounded rendered output, not an unbounded Tool Result.

## Local operator surface

- `TerminalManager` also owns a local-operator projection across all active Terminal Sessions. It exposes command, owner, PID, state, dimensions, timestamps, working directory, attention summary, and integration metadata without exposing Tool-only arguments or artifact paths; this projection does not weaken the exact-owner authorization of Agent-facing `terminal_beta` calls.
- Each Terminal Session owns a bounded sequenced `ReplayEventStream`. A new WebUI watcher receives one authoritative ANSI snapshot of the rendered screen and current sequence, then ordered raw PTY deltas plus state projections; a missing sequence requires rebuilding from a fresh snapshot rather than guessing at VT state.
- The app-wide server socket receives `resource_changed(kind="terminals")` only for active-catalog or projected-state changes. High-volume PTY output stays on `/ws/terminals/{terminal_id}` and never enters the shared bus, Session history, or model context. Operator input, resize, and kill are RPC mutations, not WebSocket commands.
- The WebUI is an observer, not a lifetime owner. Opening, navigating away from, reconnecting, or closing the view never ends a Terminal Session; only the existing lifecycle boundaries do. Confirmed operator stop terminates only the selected process tree and suppresses redundant exit attention.
- `TerminalRenderer.ansi_snapshot()` reconstructs the current pyte screen as VT/ANSI state including cell styles, indexed/RGB colors, and cursor position. This authoritative reconstruction lets xterm.js recover after navigation or a stream gap without replaying an unbounded raw log.

## Codex integration

- A command whose executable basename is `codex` receives invocation-local options for inline-screen rendering, startup-update-dialog suppression, hook trust, and the `default_mode_request_user_input` feature. vBot does not edit the user's Codex configuration and does not use non-interactive Codex execution.
- Invocation-local hooks send `PermissionRequest`, matching `PreToolUse` for `request_user_input`, and `Stop` events to `core.tools.terminal_hook_sink`. The sink reads bounded JSON from stdin, authenticates records with a per-Terminal nonce carried through the child environment, appends one compact JSONL record, and always returns success so the side channel cannot block Codex.
- `PermissionRequest` supplies exact Tool name/input as `approval`; `request_user_input` supplies structured questions as `question`; `Stop` supplies the final Assistant message as `turn_complete`. Events are deduplicated by stable hook/turn/tool identity and rejected when the nonce or external Codex Session id does not match.
- `TriggerService.submit_completion` delivers attention to the owning vBot Session, using the Run that started the current turn as origin. The notification tells the resumed Agent to inspect/answer or re-evaluate the user goal, reuse the existing Terminal Session, and avoid starting duplicate Codex processes.
- Non-Codex TUIs still get real terminal control, status, input, resize, exit, and error attention. They have no generic structured way to distinguish an arbitrary on-screen question from ordinary output; screen-text heuristics are not an attention correctness boundary.

## Lifecycle integration and tests

- Runtime creates `TerminalManager` after `TriggerService`, registers `terminal_beta`, injects the manager into `CommandDispatcher`, and stops it before temporary-file cleanup. Run cancellation does not own or cancel Terminal Sessions.
- A successful `/agent` Session move transfers matching Terminal ownership to the destination address. Session deletion, Identity Agent deletion, and Project removal terminate matching Terminal Sessions inside their admission-guarded workflows.
- Unit coverage lives in `tests/core/tools/test_terminal_manager.py`, `test_terminal_beta.py`, and `test_terminal_hook_sink.py`; Runtime, Storage, Command, RPC, WebSocket, and WebUI tests cover registration, retention paths, transfer, deletion cleanup, operator projection/control, stream sequencing, and reconnect. Real Windows validation must use an actual interactive Codex TUI through ConPTY because pipe-based tests cannot prove TTY rendering, hook delivery, resize, or process-tree stop behavior.
