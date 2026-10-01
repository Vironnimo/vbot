# Server, Paths, Update, Uninstall, Autostart, Desktop, Doctor

These commands run locally. Management commands in other areas normally contact the server; data-store maintenance also has local operations described in `data-store.md`.

## Select the instance

- For RPC-backed management commands, append `--host <host> --port <port>` after the command to select the server. Keep those options on subsequent calls; the CLI does not remember a target from the previous command.
- Host normally defaults to `127.0.0.1`. Port resolves from `--port`, then `VBOT_SERVER_PORT`, then the selected local Settings, then `8420`.
- The local data directory resolves from `--data-dir`, then `VBOT_DATA_DIR`, then an applicable checkout/worktree marker, then `~/.vbot`. Use `vbot home [--data-dir <path>]` to inspect it. `--data-dir` selects local configuration and lifecycle state; it is not sent to the server to redirect a management request.
- Keep local lifecycle commands on the machine that owns the server. For a remote server, management uses its host/port; local paths still refer to the machine running the CLI. Do not treat `home`, `doctor`, or local snapshot results as remote filesystem observations.
- `home` and `doctor` accept only `--data-dir`; `desktop` accepts only host/port and has its own last-used-server default. `update` and `uninstall` take no target options: they act on the installation that runs the command, with its recorded server and data directory.

## Server lifecycle

```bash
vbot server start
vbot server stop
vbot server restart
vbot server status
```

- `start` refuses to launch over a non-vBot process on the target port. Don't kill the occupant — report the conflict or target a different port/data-dir.
- `status` can exit 0 when the server is stopped or a non-vBot process occupies the port. Read the reported state; exit 0 alone does not establish readiness.
- On a Linux installation with Autostart, these commands control the systemd user unit `vbot.service`. Use them instead of `systemctl` or killing processes.

## Paths

```bash
vbot home [--data-dir <path>]
```

Prints the absolute `vbot_root` of the running checkout/install and the resolved `data_dir`. This is local and read-only; it does not report a Project cwd or Agent Workspace and needs no server.

## Update

```bash
vbot update [--no-restart] [--detach]
vbot update status [<operation-id>]
vbot update activate <operation-id>
vbot application status
vbot application channel main|release
```

Every vBot installation updates from the signed packages of its update channel: `release` installs published releases, `main` the newest build of vBot's main branch. `vbot application status` shows the channel. `vbot application channel main|release` switches it; the next `vbot update` applies it. Change the channel only when the user asks.

First inspect `vbot home`. Its `application_root` field identifies the installation; `vbot_root` is the readable active version, never edit it. Without `application_root`, this vBot runs from a development checkout: `update`, `uninstall`, `autostart` and `application` refuse there. A development checkout is updated with git; report that to the user instead of updating it yourself.

Run `vbot update` directly. It returns a saved operation id before this Run's server stops and automatically arranges a continuation in the same Session. After acceptance, end this Run; the updater may cancel it once this Tool batch is saved. Do not create a Bootstrap for the update. The continuation must inspect `vbot update status <operation-id>` before reporting success; acceptance is not completion. When the channel publishes no newer version, the update reports that vBot is already up to date and the server keeps running.

Normal output uses readable progress and one outcome summary. `--output plain` returns the structured update result without progress; `vbot update status <operation-id>` is a read-only structured inspection. Human terminal callers normally wait for the final outcome. `--detach` returns after acceptance. Closing the terminal or the originating shell command does not cancel the independent update. Tray updates use the same operation and do not create an Agent. The updater waits for accepted work to drain; do not stop the server manually to bypass that wait.

`vbot update --no-restart` prepares a version without changing the active one. Activate it later with `vbot update activate <operation-id>`. `--package <path>` deliberately selects a local package; official downloads require the installation's trusted signing key. A Desktop Client update has no local server target. Open Desktop windows retain their current version until reopened.

Inspect the saved phase, message and error after a failure. `rolled_back` means the previous code was verified and retained; its message also states whether the updater restored the data from the snapshot it took before the update, found the data unchanged, or did not restore it and why. `needs_attention` means versions and data were preserved for inspection. If its message says a data restore did not complete, no version can start until the printed `vbot data-store snapshot restore <snapshot-id> --all --yes` completes; report this to the user and run it only when the user asks. Never restore a data snapshot on your own initiative.

## Additional Python packages

User Extensions remain in the server data directory and keep their existing reload workflow. For additional Python packages, provide a complete requirements file to `vbot application dependencies install --requirements <path>`, inspect `vbot application dependencies status`, then restart the server. This replaces the managed dependency recipe and checks compatibility with the base runtime; never run pip into an installed version. Official updates prepare that recipe for their new runtime before stopping the current server.

## Uninstall

```bash
vbot uninstall
vbot uninstall (--app-only|--data-only|--all) --yes
```

With an interactive terminal, the bare command asks whether to remove only the application, only the data directory, or both. Data removal displays the exact resolved path and requires typing `DELETE`; it permanently removes settings, credentials, Agents, Sessions, and all other runtime state. A data-only reset preserves the prior server state: running restarts fresh, stopped remains stopped. Non-interactive callers must select one mode and pass `--yes`. A Desktop Client owns no server data.

On Windows the installation's registered uninstaller removes the application; the CLI may report only that it was launched, so wait for removal before claiming completion. On Linux the command itself removes the systemd unit, the `vbot` command link and the installation directory; run it from a directory outside the installation. Both keep the data directory unless the selected mode removes it.

## Autostart

```bash
vbot autostart enable|disable|status
```

- Windows registers a per-user logon task that runs `vBot.exe`, which opens the tray and starts the owned server; a Desktop Client has no server. It does not require an elevated terminal; run installation and lifecycle commands from a normal PowerShell so data and server processes remain user-owned. There is no Windows service.
- Linux registers the systemd user unit `vbot.service`, which runs the server and restarts it after a crash, and enables login lingering so it starts at boot. When lingering cannot be enabled, `enable` reports it and the server starts only once the user logs in; then tell the user to run `sudo loginctl enable-linger <user>`.
- `enable` registers future startup; use `vbot server start` to start now. `disable` removes the registration and leaves a running server untouched.

## Desktop

In a Windows installation, this launches the separate Desktop process and returns. A server-with-Desktop installation opens its recorded local server by default; a Desktop Client retains the last-used-server behavior. Explicit host/port options are forwarded. Closing Desktop leaves the server and tray running. Linux installations have no Desktop. The blocking behavior below applies to launches from a development checkout.

```bash
vbot desktop [--host <host>] [--port <port>]
```

Opens the native desktop window (pywebview shell) pointed at a local or remote server. Purely a local GUI launch: it does not start or manage a server and takes no `--data-dir`. Without flags it auto-connects to the last-used server (or shows the connection screen on first run). The command blocks until the window is closed. Requires the `[desktop]` dependency group — otherwise it prints an install hint and exits non-zero.

## Doctor

```bash
vbot doctor settings [--data-dir <path>]
vbot doctor config [--data-dir <path>]
```

Local validation with file/path diagnostics; no server needed. Doctor takes only `--data-dir`, no `--host`/`--port`. Run `doctor config` after any manual JSON edit.

- `settings` checks `settings.json` only.
- `config` checks every JSON document vBot keeps in the data directory: `settings.json`, Agent configs and the Agent order, Project files, Channel configs, Cron and Bootstrap jobs, Calendar events and actions, the Skill policy, Terminal groups and launch history, System Prompt layouts, OAuth token files, MCP connections, and attachment and speech metadata. It does not check the databases; `vbot data-store status` reports their health (`data-store.md`).

Reading the `doctor config` report:

- The header shows `doctor config: ok` or `doctor config: failed`, the `data_dir`, `files_checked`, and `errors:` and `warnings:` counts when there are any. A final line summarizes the result.
- Each document then gets one line, `<path>: valid`; a missing `settings.json` shows as `missing (defaults will be used)`. A directory with five or more valid documents, typically attachment metadata, shows as one line `<dir>/: N documents valid`.
- A document with problems is always listed by name, `<path>:`, followed by one line per problem: `- error <json-path>: <message>` or `- warning <json-path>: <message>`.
- Errors fail the command; warnings do not. An unknown field is a warning: vBot ignores it but keeps it when it rewrites the file.
- A `format_version` error saying the version is required means the file comes from a vBot release before the current data format and the whole data directory needs the one-time offline conversion; a version newer than this vBot reads means a newer vBot wrote the file. Do not edit `format_version`; report either case to the user.
