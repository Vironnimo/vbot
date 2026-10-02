# Desktop update restart

Read when changing how a packaged Desktop notices a newly activated version, restarts into it, or restores its window afterwards. Owner: `desktop/restart.py` (`DesktopRestart`), wired in `desktop/main.py`; the successor claim lives in `desktop/_windows.py`. The WebUI side (banner, idle preparation) is `webui/app-shell.md`; version activation is `cli/application.md`.

## Relaunch contract

- Only a packaged launch restarts. `cli/application/desktop.py` (`open_desktop`) passes `VBOT_DESKTOP_RELAUNCH` = JSON `{version_file, version, command}`: the installation's absolute `active-version` path, the version id this Desktop runs, and the command that launches the active version (`[<install>/vBot.GUI.exe, "desktop"]`, `vBot.exe` when the GUI companion is missing). `restart.relaunch_contract()` accepts it only when the file and the command's first element are absolute, the version id is safe and the command is non-empty; anything else (and every source run) means no restart support, and the launch never reads a restart request.
- The relaunch goes through the stable bootstrap, never a versioned path, so the successor always runs whatever version is active at that moment. `desktop/restart.py` spawns it itself (new process group, no window, breakaway from the job when allowed, all std streams to `DEVNULL`) because the Desktop imports no project code.

## Pending detection and status

- A daemon watcher (`vbot-desktop-restart`, started after the window is shown, stopped first on shutdown) checks the version file right away and then every `VERSION_POLL_SECONDS` (5 s); an unchanged mtime/size is not re-read. The Desktop is *pending* while the active id differs from the running one. Each change publishes the status and resets `failed` (a failure belongs to the version it tried to start).
- Status `{pending, restarting, failed}` reaches the page as `getDesktopUpdate()` and the push `vbot-desktop-update` (one waiting snapshot, replaced by a newer one).

## Restart triggers

- **Requested:** `restartDesktop()` starts the handoff and resolves `{accepted: true}` immediately; the outcome arrives as status pushes. It rejects with `no_update_pending` when nothing newer is active and `restart_unavailable` without a relaunch contract. A second request during a handoff is a no-op. The WebUI flushes autosave and the composer before calling it.
- **Idle:** due when pending, not restarting, the window has not been in the foreground for `IDLE_AFTER_SECONDS` (10 min; `_windows.foreground_is_own_process`, always true off Windows so no idle restart there) and Voice is neither recording nor calibrating (`VoiceController.is_busy`; an error counts as busy). The page is asked first with the cancelable push `vbot-desktop-restart` (`detail` `{reason: "idle"}`, at most one waiting). A page that acknowledges with `preventDefault()` takes over: it prepares and calls `restartDesktop()` itself, or declines by doing nothing (for example during a Live call); the Desktop asks again after `PAGE_RETRY_SECONDS` (5 min). An unhandled push (connection screen, an older remote WebUI without the handler, no window) restarts right away.
- A failed handoff sets `failed` and delays the next automatic attempt by `FAILED_RETRY_SECONDS` (10 min); a manual request may retry at once.

## Handoff protocol

1. The old Desktop writes `<config-dir>/restart-request.json` atomically: `{created_at, nonce, server: {host, port}, location: "#...", window: {state, width, height, x?, y?}}` (nonce = 32 hex characters, fresh per attempt), then runs the relaunch command.
2. The successor (`launch_desktop`, contract present) takes the request (reads and deletes it). It is valid for 120 s in either direction, at most 16 KiB, with an alphanumeric nonce of 16-64 characters; an invalid server, location or placement is dropped individually and the nonce alone still completes the handoff. A request server replaces any launch target and Session link.
3. The successor calls `claim_desktop_instance(handoff=nonce)`: finding the guard held, it writes the activation request `{"handoff": nonce}`, signals, and polls the mutex for up to `HANDOFF_CLAIM_TIMEOUT_SECONDS` (30 s). Without the guard by then it logs a warning and exits without a window (`launch_desktop` returns `False`).
4. The old Desktop's activation listener routes a `handoff` request to `DesktopRestart.accept_handoff`, never to focusing; only the current attempt's nonce counts. It then destroys its window; the normal shutdown releases the guard.
5. The successor claims the guard and opens the request's server with the location appended to the first navigation, at the recorded placement. A server still starting after the update is waited for (`desktop.md` -> Interfaces -> Launch wait).

The old window closes only after step 4. Without the signal within `HANDOFF_TIMEOUT_SECONDS` (60 s), or when the relaunch process exits nonzero first, the attempt fails: the request is deleted (only when it still holds this nonce), the window stays, and the page shows the failure. Closing the Desktop during a handoff abandons it and deletes the request.

## Restored state

- **Server:** `ConnectionController.active_target()` (the last prepared connect). The connection screen restores as the saved-server default.
- **Location:** the page URL fragment (`get_current_url()`, only when it belongs to the active server), printable, whitespace-free, at most 2048 characters, starting with `#`. The WebUI owns what a fragment means.
- **Placement:** logical size and position for a normal window; a minimized or maximized window records only its state and the remembered normal size (its reported position is meaningless, e.g. Windows' -32000), and the successor centers it and opens it in that state. Positions outside -20000..100000 are dropped. The placement is not persisted to `settings.json`; the normal close still stores the size.

## Gotchas

- Restart only takes effect for Desktops launched by an installation whose `open_desktop` passes the contract; a Desktop started by older code has no contract and keeps running its version until closed.
- The successor must not touch the page or Voice before it owns the guard: two processes would share the WebView2 profile and compete for the microphone and the global shortcut.
- Idle detection is foreground-based: a window left in front never restarts on its own, by design.

## Tests

`tests/desktop/test_restart.py` (contract validation, request round trip and expiry, pending detection, handoff success and failure, idle matrix), `test_windows.py` (successor claim), `test_main.py` (successor launch restoring server, location and placement), `test_page_events.py` (update and restart pushes), `test_bridge.py` (restart capability and methods), `tests/cli/application/test_host.py` (relaunch contract from `open_desktop`).
