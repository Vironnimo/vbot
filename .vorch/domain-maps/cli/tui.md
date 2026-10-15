# Terminal Conversations

Read this when changing the interactive terminal accessor, its Textual interface, transport or recovery. The shared semantic timeline is owned by `chat/timeline.md`; ordinary CLI commands remain covered by `cli.md`.

## Entry and boundaries

`vbot tui [--agent <address>] [--session <id> | --project <id>]` opens a full-screen Textual application. It accepts the CLI's resolved `--host`, `--port` and `--data-dir` target. Terminal stdin and stdout are required; automation keeps using one-shot `vbot chat`. Parser/help paths do not import Textual. `_dispatch_tui.py` lazily starts `cli/tui`; `app.py` owns interaction with composer actions in `composer.py`, `state.py` local conversation state and wire patch application, `rendering.py` timeline widgets, `dialogs.py` pickers/forms, and `transport.py` asynchronous HTTP/SSE/WebSocket transport.

The TUI calls the existing server contracts; it owns no Provider execution, durable Sessions or Queue. Internal `actions.py` exposes Session/Tool operations and `models.py` owns Model/effort selection; `drafts.py` owns local draft persistence and conflict preservation. The supported remote path is SSH or an SSH tunnel to the HTTP target. `textual` is in the CLI and development dependency groups, and packaged server runtimes include the CLI group on Windows and Linux.

## Interaction

The main screen shows one conversation, a multiline composer and compact state/Usage. Searchable pickers select Agents, Projects and Sessions. Drafts, pending attachments, reading positions and navigation remain local per conversation. Reasoning and Tool details expand on demand; Markdown/code remain text. File actions distinguish local uploads/download destinations from server file references. Extension forms use terminal controls; URL requests and Extension pages expose links for a browser.

F1 documents the available bindings. Ctrl+S/Alt+Enter sends; Enter adds a line. F2 selects a Session, F3 starts a conversation, F4 controls Model/effort, F5 shows activity, F6 the shared Queue/Steer, F7 Extension requests, F8 files, F9 Commands/Skills. Ctrl+X stops the current Run, F10 invokes Stop all, F11 opens Session/Tool actions and saved drafts, F12 returns to latest output, Ctrl+B navigates to the Parent/back, Ctrl+O loads older history, Ctrl+Space completes commands/files, and Ctrl+Q detaches. Modal controls must not invoke hidden main-screen mutations.

Server file snapshots are resolved from the captured submitted text through `files.list`, including quoted paths and explicitly browsed ignored files. The completion cache is never the submission authority. Local paths use a browser/typed-path dialog; downloads request original bytes and exclusively create their destination. Extension refusal preserves entered values and displays the server error. An unknown form outcome never triggers an automatic resubmission.

F4 shows the effective Agent/Team or Session Model and effort before selection. Effort choices use `model.get`'s effective Connection ladder and accepted settings values; a pinned Connection with an account suffix retains that Connection's capabilities. An unavailable pin does not borrow another Connection's ladder.

`chat.work.running` supplies current Sub-Agent and background-command activity independently of loaded History; timeline rows additionally retain completed work. Opening a Child Session is read-only navigation; sending follows existing reply-target rules. Queue editing/removal/Steer remain server operations. Stop Run, Stop all and closing the accessor are distinct actions. Copy/export operates on loaded history; arbitrary HTML, Voice and embedded terminal image/video protocols are outside this accessor.

## Recovery and reply surface

`TerminalClient` maintains HTTP and `/ws` presence (`accessor=tui`), with a timeline stream for the displayed Run. Reconnect starts from authoritative server facts and a semantic snapshot. Selection/read generations keep late responses from overwriting another conversation. Patch base mismatches require a new snapshot. No mutation is automatically retried; an uncertain send stays visibly uncertain until inspection/reconciliation, preventing duplicate admission after a lost response.

History catch-up drains every newer page before publishing the snapshot. Timeline rows use immutable references and retain unchanged widgets; streaming updates do not deep-copy the entire loaded transcript. Completion is acknowledged only after that exact completed Run is rendered at the tail of the focused conversation, outside a modal. Scoped rename, move and deletion events preserve input while updating the addressed conversation.

Drafts, attachments, selected overrides and pending-send/edit recovery survive restart in `~/.vbot-tui-state/<server-url-sha256>.json` on the terminal computer, outside the server data directory. `drafts.py` uses Generation 1 JSON helpers, preserves unknown fields, refuses invalid/newer documents, and merges under a cross-process file lock. Conflicting edits from two accessors retain both drafts. Before sending, the addressed submission is checkpointed as uncertain; a new conversation supplies `new_session.session_id` so a lost response remains discoverable by read-only recovery. F11 restores ordinary drafts; uncertain edits are reconciled explicitly through the Edit action. Restoring never submits input.

Interactive sends identify `reply_surface=tui`; Command catalogs use the same surface. Its Model reminder describes terminal Markdown/code and file links without embedded media. A delivered Steer changes the subsequent reply surface; merely opening a terminal does not. Settings lists connected TUI accessors as Terminal. Closing the app releases local connections and leaves server Runs running.

## Agent-facing discovery

The bundled `vbot-docs/references/chat.md` distinguishes human-operated `vbot tui` from script/Agent `vbot chat`. Its explicit prohibition on launching the TUI from an automated shell Tool prevents a blocking interactive process; it does not restrict a human terminal invocation.

## Verification

Parser/dispatch and non-terminal refusal: `tests/cli/test_cli_main.py`. TUI interaction and transport contracts live under `tests/cli/`; use Textual's mounted test driver for input, modal isolation, streaming, Queue and navigation. Live acceptance uses only the isolated checkout target and the workflows linked from `PROJECT.md`.
