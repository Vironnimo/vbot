# Windows Tray Host

Supplementary to `cli/windows-application.md`. Read when changing the `vBot.exe` tray: its state, menu, status window, toasts, or its server connection.

## Owners

- `host.py` `ApplicationFacade`: the tray's view of one installation (`state()`, lifecycle actions, `open_session`, update progress lines, status rows). `watch(sink)` creates the monitor and notifier; `main()` keeps the startup order (recover operations, optional initial start, tray).
- `tray.py` `TrayController`: projects `TrayState` into `TrayPresentation` (icon state, tooltip, menu, status window) and runs actions on one worker thread. Toolkit-free; tests drive it with fake views.
- `monitor.py` `ServerMonitor`: one asyncio thread holding the `/ws` connection, the `/health` classification and an `httpx.AsyncClient` for RPC.
- `notifications.py` `Notifier` and `run_toast_kind`: which events deserve a toast and what it says.
- `windows_tray.py` `WindowsTray`: hidden host window, notification icon, owner-drawn popup menu, toasts, icon badges. `windows_status.py` `StatusWindow`: the status window. `windows_native.py`: private `WinDLL` instances with full prototypes, theme palette and drawing helpers. All Win32 calls run on the tray's main thread; other threads enqueue work and post one wake message.
- `integration.py` `register_notification_identity` / `remove_notification_identity`: the toast identity in the registry.

## Idle cost contract

The tray does no periodic server I/O. Server state comes from the event stream; `/health` is probed only after a failed connect. The controller's 1 s poll only calls `facade.state()`, which must stay cheap (about 0.15 ms): `operations.OperationObserver` rereads only operation records whose mtime/size changed, and the release version and server URL are cached. Do not add network, process or full-directory work to `state()`. Measured idle cost: about 0.02 % of one core (previously 0.5-3 % from a per-second health probe with a fresh HTTP client plus reloading every operation record).

## Server connection

- URL `ws://<target>/ws?connection_id=<stable hex>&accessor=tray`, plus `epoch` and `after_sequence` when reconnecting within 60 s of a loss. Heartbeat frames are ignored; protocol pings (20 s) are the only idle traffic. Protocol details: `server/events-and-reconnect.md`.
- Target: the owned server (`processes.target`) for server shapes; a Desktop Client follows the Desktop's last used server and re-checks it every 30 s while connected.
- Retry backoff 1-5 s for a local target, up to 30 s for a remote one. `start_server`/`restart_server` skip the current delay.
- State mapping in the facade: connected -> running; refused or unreachable -> stopped; rejected by a listener whose `/health` is exactly vBot -> running (safe mode); rejected by anything else -> conflict; before the first attempt -> unknown.
- Close codes 1000, 1001 and 1012 are cooperative. Any other loss (a killed process shows 1006) arms the server-stopped toast, which fires only if the next connect is refused.

## Toasts

- Kinds and switches: Settings `notifications.run_completed`, `run_failed`, `automation_failed`, `update_result`, `server_stopped` (all default on), read through `settings.values` on each connect and before each Run toast.
- Run toasts: only `user` and `system` Runs whose event does not carry `contributes_to_agent_activity: false`; Cron and Calendar Runs only on failure; `run_interrupted` counts as failed; cancelled never. Title names the Agent (`agent.get` name for Identity Agents, the id for Project Agents); the body is the Session title (`session.get`) plus the first error line.
- "Already looking at it": Run toasts wait 1.5 s. A `resource_changed` `sessions` event carrying `read_run_id` for that Run cancels the pending toast or dismisses the shown one. The WebUI acknowledges only while its page is visible and focused (`webui/chat.md`).
- Server stopped: only server-owning shapes, never after a tray-requested stop or restart (`expect_server_stop`) or while an update runs; dismissed when the stream reconnects.
- Update result: fired once when the facade observes an operation's terminal state, together with the matching status-window lines. The newest operation is history, without lines or toast, when it is already terminal at the tray's first observation.
- Mechanics: legacy `NIF_INFO` balloons, which Windows 10/11 shows as toasts. The process sets AppUserModelID `vBot.Tray`; `HKCU\Software\Classes\AppUserModelId\vBot.Tray` (DisplayName, IconUri) names them "vBot". The tray writes it at every start (last started installation wins); `begin_removal` deletes it only while its IconUri points into the removed installation.
- Click opens the Session through `facade.open_session`: the `server` shape opens the browser at `<url>/?open_agent=...&open_session=...`; Desktop shapes launch Desktop with explicit `--host/--port` and `--open-session AGENT SESSION` (contract in `desktop.md`). Toasts without a Session open the status window.
- While a toast shows, a foreground WinEvent hook (skipping the tray's own process) dismisses it when a `vBot.Desktop.exe` window becomes foreground.

## Menu, icon and status window

- Left click (`NIN_SELECT`/`NIN_KEYSELECT`) runs the default item: Open Desktop for Desktop shapes, Open in browser for a running `server` shape, otherwise Status. Right click (`WM_CONTEXTMENU`) builds the owner-drawn popup at open time.
- The menu follows the Windows light/dark app theme and DPI. Lifecycle items and Quit are disabled while an update runs. Application logs and Server logs open their respective owners. External tray shutdown ends an open popup before destroying the window.
- Busy-cursor gotcha: the host window class carries the arrow cursor and the tray calls `SetCursor` before `TrackPopupMenuEx`; without both, Windows shows the busy cursor while hovering the menu.
- Icon states normal, stopped, updating and error are badges rendered with Pillow over the release icon. The tooltip is at most 127 characters.
- The status window shows version, rows (Server, Data or the Desktop Client's server, Updates from, Last update, Installed in), the update activity since tray start (the same progress lines as the CLI plus the final summary from `operations.result_summary`) and up to three action buttons.

## Tests

`tests/cli/application/test_tray.py` (projections, controller, Win32 menu/toast/badge behavior with patched `user32`), `test_host.py` (state mapping, update lines and toast, expected stop, `open_session`), `test_monitor.py` (loopback `/ws` server: accessor, heartbeat skip, resume cursor, failure classification), `test_notifications.py` (toast policy and texts), `test_integration.py` (toast identity removal).
