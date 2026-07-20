# Server, Update, Uninstall, Autostart, Desktop, Doctor

These are the only CLI areas that work without a running server.

## Server lifecycle

```bash
vbot server start
vbot server stop
vbot server restart [--service-name <unit>]
vbot server status
```

- `start` refuses to launch over a non-vBot process on the target port. Don't kill the occupant — report the conflict or target a different port/data-dir.
- On a systemd-managed Linux install, `restart` is routed through the service unit (default `vbot`, override with `--service-name`) so it does not fight the unit.

## Update

```bash
vbot update [--discard | --stash] [--no-restart] [--service-name <unit>]
```

Updates the installation from the git checkout it was installed from, then restarts the server. Never touches the `~/.vbot` data directory.

- The track is auto-detected: a branch checkout pulls and rebuilds the WebUI locally (needs Node); a release-tag checkout fetches the latest release with its prebuilt WebUI (no Node, re-downloaded only when the tag changed).
- With local changes to tracked files, `update` refuses. `--discard` drops them; `--stash` keeps them and reapplies after the update.
- `--no-restart` updates the code without restarting.

## Uninstall

```bash
vbot uninstall [--task-name <name>] [--service-name <name>]
```

Removes the application, managed environment, launchers, and Autostart while preserving the vBot data directory. Windows requests UAC and finishes in a separate PowerShell window because the running CLI cannot delete its own executable; cancelling elevation leaves the installation in place. Pass the matching custom Autostart name only when the installation did not use the default.

## Autostart

```bash
vbot autostart enable|disable|status [--task-name <name>] [--service-name <name>]
```

- `enable` registers OS autostart and starts the server now: a Windows Task Scheduler logon task (`--task-name`, default `vBot`) or a Linux systemd user unit (`--service-name`, default `vbot`) with login lingering.
- Windows gotcha: creating the task needs an elevated (Administrator) terminal.
- `disable` removes the entry but leaves a running server untouched.

## Desktop

```bash
vbot desktop [--host <host>] [--port <port>]
```

Opens the native desktop window (pywebview shell) pointed at a local or remote server. Purely a local GUI launch: it does not start or manage a server and takes no `--data-dir`. Without flags it auto-connects to the last-used server (or shows the connection screen on first run). The command blocks until the window is closed. Requires the `[desktop]` dependency group — otherwise it prints an install hint and exits non-zero.

## Doctor

```bash
vbot doctor settings [--data-dir <path>]
vbot doctor config [--data-dir <path>]
```

Local validation with file/path diagnostics; no server needed. `settings` checks `settings.json` only; `config` checks the full user-editable JSON bundle (settings, agents, channels, cron jobs). Run `doctor config` after any manual JSON edit. Doctor takes only `--data-dir`, no `--host`/`--port`.
