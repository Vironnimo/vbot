# Terminal Conversations

Read this when changing the interactive terminal accessor, its Textual interface, transport or recovery. The shared semantic timeline is owned by `chat/timeline.md`; ordinary CLI commands remain covered by `cli.md`.

## Entry and boundaries

`vbot tui [--agent <address>] [--session <id> | --project <id>]` opens a full-screen Textual application. It accepts the CLI's resolved `--host`, `--port` and `--data-dir` target. Terminal stdin and stdout are required; automation keeps using one-shot `vbot chat`. Parser/help paths do not import Textual. `_dispatch_tui.py` lazily starts `cli/tui`; `app.py` owns interaction with composer actions in `composer.py`, `state.py` local conversation state and wire patch application, `rendering.py` timeline widgets, `dialogs.py` pickers/forms, and `transport.py` asynchronous HTTP/SSE/WebSocket transport.

The TUI calls the existing server contracts; it owns no Provider execution, durable Sessions or Queue. The supported remote path is SSH or an SSH tunnel to the HTTP target. `textual` is in the CLI and development dependency groups, and packaged server runtimes include the CLI group on Windows and Linux.

## Interaction

The main screen shows one conversation, a multiline composer and compact state/Usage. Searchable pickers select Agents, Projects and Sessions. Drafts, pending attachments, reading positions and navigation remain local per conversation. Reasoning and Tool details expand on demand; Markdown/code remain text. File actions distinguish local uploads/download destinations from server file references. Extension forms use terminal controls; URL requests and Extension pages expose links for a browser.

F1 documents the available bindings. Ctrl+S/Alt+Enter sends; Enter adds a line. F2 selects a Session, F3 starts a conversation, F4 controls Model/effort, F5 shows activity, F6 the shared Queue/Steer, F7 Extension requests, F8 files, F9 Commands/Skills. Ctrl+X stops the current Run, F10 invokes Stop all, Ctrl+B navigates to the Parent/back, Ctrl+O loads older history, Ctrl+Space completes commands/files, and Ctrl+Q detaches. Modal controls must not invoke hidden main-screen mutations.

`chat.work.running` supplies current Sub-Agent and background-command activity independently of loaded History; timeline rows additionally retain completed work. Opening a Child Session is read-only navigation; sending follows existing reply-target rules. Queue editing/removal/Steer remain server operations. Stop Run, Stop all and closing the accessor are distinct actions. Copy/export operates on loaded history; arbitrary HTML, Voice and embedded terminal image/video protocols are outside this accessor.

## Recovery and reply surface

`TerminalClient` maintains HTTP and `/ws` presence (`accessor=tui`), with a timeline stream for the displayed Run. Reconnect starts from authoritative server facts and a semantic snapshot. Selection/read generations keep late responses from overwriting another conversation. Patch base mismatches require a new snapshot. No mutation is automatically retried; an uncertain send stays visibly uncertain until inspection/reconciliation, preventing duplicate admission after a lost response.

Interactive sends identify `reply_surface=tui`; Command catalogs use the same surface. Its Model reminder describes terminal Markdown/code and file links without embedded media. A delivered Steer changes the subsequent reply surface; merely opening a terminal does not. Settings lists connected TUI accessors as Terminal. Closing the app releases local connections and leaves server Runs running.

## Agent-facing discovery

The bundled `vbot-docs/references/chat.md` distinguishes human-operated `vbot tui` from script/Agent `vbot chat`. Its explicit prohibition on launching the TUI from an automated shell Tool prevents a blocking interactive process; it does not restrict a human terminal invocation.

## Verification

Parser/dispatch and non-terminal refusal: `tests/cli/test_cli_main.py`. TUI interaction and transport contracts live under `tests/cli/`; use Textual's mounted test driver for input, modal isolation, streaming, Queue and navigation. Live acceptance uses only the isolated checkout target and the workflows linked from `PROJECT.md`.
