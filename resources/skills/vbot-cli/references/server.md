# Server, Paths, Update, Uninstall, Autostart, Desktop, Doctor

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

## Paths

```bash
vbot home [--data-dir <path>]
```

Prints the absolute `vbot_root` of the running checkout/install and the resolved `data_dir`. This is local and read-only; it does not report a Project cwd or Agent Workspace and needs no server.

## Update

```bash
vbot update [--discard | --stash] [--no-restart] [--service-name <unit>]
```

Before an update that will restart the server, create a one-shot Bootstrap in the current Session and verify that it was saved before starting the update:

```bash
vbot bootstrap create --current-session --name "Verify vBot update" --mode once --prompt "The vBot update that interrupted this Session should now be complete. Use the vbot CLI to run vbot server status, vbot log list, and vbot log read on the latest log. Verify startup and update health, investigate relevant errors if present, then report clearly whether the update succeeded and what needs user attention. Do not repeat the update."
vbot bootstrap list
vbot update
```

`--current-session` works only inside a vBot Run Bash command. It binds both the current Agent address and Session without guessing from an Agent's default Session. The created job is armed for the next startup and cannot fire in the current process. Use `vbot bootstrap list` to confirm its `mode=once`, `status=active`, and Session before running the update. If the user requested `--no-restart`, do not create this Bootstrap unless a later startup check is explicitly wanted.

Updates the installation from the git checkout it was installed from, then restarts the server. Never touches the `~/.vbot` data directory.

- The track is auto-detected: a branch checkout pulls and rebuilds the WebUI locally (needs Node); a release-tag checkout fetches the latest release with its prebuilt WebUI (no Node, re-downloaded only when the tag changed).
- On Windows, close every vBot Desktop window first. The updater refuses before changing the checkout when this installation's exact Desktop launcher is running and checks again before pip; follow the printed source-based `resume update` command if an earlier pip failure damaged the normal `vbot` launcher.
- With local changes to tracked files, `update` refuses. `--discard` drops them; `--stash` keeps them and reapplies after the update.
- `--no-restart` updates the code without restarting.

## Uninstall

```bash
vbot uninstall
vbot uninstall (--app-only|--data-only|--all) --yes [--host <host>] [--port <port>] [--data-dir <path>]
```

With an interactive terminal, the bare command asks whether to remove only the application, only the data directory, or both. Data removal displays the exact resolved path and requires typing `DELETE`; it permanently removes settings, credentials, Agents, Sessions, and all other runtime state. A data-only reset preserves the prior server state: running restarts fresh, stopped remains stopped. Non-interactive callers must select one mode and pass `--yes`.

The target defaults to the Installer-recorded server host, port, and data directory; the target flags override it. Application removal deletes the managed environment, launchers, and Autostart. Windows requests UAC and finishes that removal in a separate PowerShell window because the running CLI cannot delete its own executable; cancelling elevation leaves both application and data in place. Pass the matching custom `--task-name` or `--service-name` only when the installation did not use the default.

## Autostart

```bash
vbot autostart enable|disable|status [--task-name <name>] [--service-name <name>]
```

- `enable` registers OS autostart and starts the server now: a low-privilege per-user Windows Task Scheduler logon task (`--task-name`, default `vBot`) or a Linux systemd user unit (`--service-name`, default `vbot`) with login lingering.
- Windows Autostart uses the current user's interactive token and does not require an elevated terminal; run installation and lifecycle commands from a normal PowerShell so checkout, data, and server processes remain user-owned.
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

Local validation with file/path diagnostics; no server needed. `settings` checks `settings.json` only; `config` checks the full user-editable JSON bundle (settings, agents, channels, cron jobs, Bootstrap jobs). Run `doctor config` after any manual JSON edit. Doctor takes only `--data-dir`, no `--host`/`--port`.
