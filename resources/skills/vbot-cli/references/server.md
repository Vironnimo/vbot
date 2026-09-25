# Server, Paths, Update, Uninstall, Autostart, Desktop, Doctor

These commands run locally. Management commands in other areas normally contact the server; data-store maintenance also has local operations described in `data-store.md`.

## Select the instance

- For RPC-backed management commands, append `--host <host> --port <port>` after the command to select the server. Keep those options on subsequent calls; the CLI does not remember a target from the previous command.
- Host normally defaults to `127.0.0.1`. Port resolves from `--port`, then `VBOT_SERVER_PORT`, then the selected local Settings, then `8420`.
- The local data directory resolves from `--data-dir`, then `VBOT_DATA_DIR`, then an applicable checkout/worktree marker, then `~/.vbot`. Use `vbot home [--data-dir <path>]` to inspect it. `--data-dir` selects local configuration and lifecycle state; it is not sent to the server to redirect a management request.
- Keep local lifecycle commands on the machine that owns the server. For a remote server, management uses its host/port; local paths still refer to the machine running the CLI. Do not treat `home`, `doctor`, or local snapshot results as remote filesystem observations.
- `home` and `doctor` accept only `--data-dir`; `desktop` accepts only host/port and has its own last-used-server default. Update, uninstall, and autostart may use installation or service metadata: inspect their command help before choosing an explicit target.

## Server lifecycle

```bash
vbot server start
vbot server stop
vbot server restart [--service-name <unit>]
vbot server status
```

- `start` refuses to launch over a non-vBot process on the target port. Don't kill the occupant — report the conflict or target a different port/data-dir.
- `status` can exit 0 when the server is stopped or a non-vBot process occupies the port. Read the reported state; exit 0 alone does not establish readiness.
- On a systemd-managed Linux install, `restart` is routed through the service unit (default `vbot`, override with `--service-name`) so it does not fight the unit.

## Paths

```bash
vbot home [--data-dir <path>]
```

Prints the absolute `vbot_root` of the running checkout/install and the resolved `data_dir`. This is local and read-only; it does not report a Project cwd or Agent Workspace and needs no server.

## Update

First inspect `vbot home`. An `application_root` field identifies a packaged installation; its `vbot_root` is the readable active version, not a development checkout. Use the packaged workflow below when that field is present. Otherwise use the source-checkout workflow that follows.

### Packaged installation

Run `vbot update` directly. It returns a saved operation id before this Run's server stops and automatically arranges a continuation in the same Session. After acceptance, end this Run; the updater may cancel it once this Tool batch is saved. Do not create an additional Bootstrap. The continuation must inspect `vbot update status <operation-id>` before reporting success; acceptance is not completion.

Normal output uses readable progress and one outcome summary. `--output plain` returns the structured update result without progress; `vbot update status <operation-id>` is a read-only structured inspection. Human terminal callers normally wait for the final outcome. `--detach` returns after acceptance. Closing the terminal or the originating shell command does not cancel the independent update. Tray updates use the same operation and do not create an Agent. The updater waits for accepted work to drain; do not stop the server manually to bypass that wait.

`vbot update --no-restart` prepares a version without changing the active one. Activate it later with `vbot update activate <operation-id>`. `--package <path>` deliberately selects a local package; official downloads require the installation's trusted signing key. `--stash` and `--discard` apply only to source checkouts. A Desktop Client update has no local server target. Open Desktop windows retain their current version until reopened.

Inspect the saved phase, message and error after a failure. `rolled_back` means the previous code was verified and retained; its message also states whether the updater restored the data from the snapshot it took before the update, found the data unchanged, or did not restore it and why. `needs_attention` means versions and data were preserved for inspection. If its message says a data restore did not complete, no version can start until the printed `vbot data-store snapshot restore <snapshot-id> --all --yes` completes; report this to the user and run it only when the user asks. Never restore a data snapshot on your own initiative or discard local features to make an update pass.

### Source-checkout installation

```bash
vbot update [--discard | --stash] [--no-restart] [--service-name <unit>]
```

Before an update that will restart the server, create a one-shot Bootstrap in the current Session and verify that it was saved before starting the update:

```bash
vbot bootstrap create --current-session --name "Verify vBot update" --mode once --prompt "The vBot update that interrupted this Session should now be complete. Use the vbot CLI to run vbot server status, vbot log list, and vbot log read on the latest log. Verify startup and update health, investigate relevant errors if present, then report clearly whether the update succeeded and what needs user attention. Do not repeat the update."
vbot bootstrap show <returned-job-id>
vbot update
```

`--current-session` works only inside a shell command of a vBot Run. It binds both the current Agent address and Session without guessing from an Agent's default Session. The created job is armed for the next startup and cannot fire in the current process. Use `vbot bootstrap show <returned-job-id>` to confirm its `mode=once`, `status=active`, and Session before running the update. If the user requested `--no-restart`, do not create this Bootstrap unless a later startup check is explicitly wanted.

The updater reports phases and elapsed time while running. Its final status distinguishes completed restart, pending restart, and an explicitly skipped restart. Read the reported state and recovery instructions; color and symbols are only presentation.

The update invocation itself stays `vbot update`. In vBot Run context the CLI internally hands the restart to a short-lived detached helper, allowing the update command and its shell launcher to finish before the old server enters normal Runtime shutdown. Do not find PIDs, stop the server first, or invoke an internal helper manually. The Bootstrap remains required because it resumes verification after the intentional process restart; it is not process-cleanup machinery.

Updates the installation from its git checkout and requests a server restart unless suppressed. In the current Run, restart is scheduled; the command’s success does not establish post-restart health. Let the saved Bootstrap verify startup. Preserves runtime data and creates a verified data snapshot of every registered database and the JSON documents before changing code. A snapshot failure stops the update. A source-checkout update never restores that snapshot, because its code cannot be rolled back automatically. If the restart fails, the result names the snapshot and the previous revision; report them to the user instead of restoring on your own initiative.

- The track is auto-detected: a branch checkout pulls and rebuilds the WebUI locally (needs Node); a release-tag checkout fetches the latest release with its prebuilt WebUI (no Node, re-downloaded only when the tag changed).
- On Windows, close every vBot Desktop window first. The updater refuses before changing the checkout when this installation's exact Desktop launcher is running and checks again before pip; follow the printed source-based `resume update` command if an earlier pip failure damaged the normal `vbot` launcher.
- With local changes to tracked files, `update` refuses. `--discard` drops them; `--stash` keeps them and reapplies after the update.
- `--no-restart` updates the code without restarting. A Desktop Client installation updates only its local client and has no server to restart.
- On a failed update, read the checkout version and completed steps before recovery. Code may already have changed even though dependency installation, WebUI build, stash reapplication or restart failed.

## Local application changes in packaged installations

Inspect `vbot application status` for the update source. A main installation reports its recorded checkout and branch. Use a managed Git worktree for isolated development and tests, then merge the intended commits into that recorded branch before `vbot update`. Keep the recorded checkout clean; updates retain local-ahead commits but stop on uncommitted changes or divergent history. Never edit the active version reported by `vbot home`.

For a managed customization of the installed version, use `vbot customize prepare` and edit only the returned development source. Add tests for the intended behavior, run `vbot customize check --intent <description>`, then activate the checked candidate with `vbot customize activate`. If the source changes after checking, check it again before activation.

The development copy starts at the exact installed source revision. Preparing and checking changes requires Git and Node.js/npm on the development machine; ordinary use and official updates do not. `customize status` reports the official base, local revision and any pending reconciliation. Tests run in a separate development environment; the release runtime is never edited in place.

`vbot customize test` runs the checked candidate with fresh data and a free local port. It suppresses Extensions and automatic Channels, Cron, Calendar and Bootstrap activity before startup; it does not copy production credentials or Sessions. Stop this foreground test before activating. Test needed external capabilities separately in a deliberately configured disposable context.

Official updates rebase local commits in a separate worktree and validate the result. When conflicts remain, inspect the saved reconciliation source, resolve and stage the changes there, then run `vbot customize rebase --intent <description>` followed by `vbot customize activate`. Do not discard a change merely because it began as a bug fix: it may express a lasting user feature.

User Extensions remain in the server data directory and keep their existing reload workflow. For additional Python packages, provide a complete requirements file to `vbot application dependencies install --requirements <path>`, inspect `vbot application dependencies status`, then restart the server. This replaces the managed dependency recipe and checks compatibility with the base runtime; never run pip into an installed version. Official updates prepare that recipe for their new runtime before stopping the current server.

## Uninstall

Packaged Windows installs use their recorded target and per-user uninstaller. The interactive command offers application-only removal, data-only reset, both, or cancellation; `--data-only` resets data while keeping the application, and `--all` removes both. Explicit target overrides must match the recorded installation. The CLI may report only that the independent uninstaller was launched; wait for removal before claiming completion. Local development copies are preserved. The following checkout-specific elevation and target-override behavior does not apply to packaged installs.

```bash
vbot uninstall
vbot uninstall (--app-only|--data-only|--all) --yes [--host <host>] [--port <port>] [--data-dir <path>]
```

With an interactive terminal, the bare command asks whether to remove only the application, only the data directory, or both. Data removal displays the exact resolved path and requires typing `DELETE`; it permanently removes settings, credentials, Agents, Sessions, and all other runtime state. A data-only reset preserves the prior server state: running restarts fresh, stopped remains stopped. Non-interactive callers must select one mode and pass `--yes`.

For a source-checkout installation, the target defaults to the Installer-recorded server host, port, and data directory; the target flags override it. Application removal deletes the managed environment, launchers, and Autostart. Windows requests UAC and finishes that removal in a separate PowerShell window because the running CLI cannot delete its own executable; cancelling elevation leaves both application and data in place. Pass the matching custom `--task-name` or `--service-name` only when the installation did not use the default.

## Autostart

Packaged Windows installations register the root `vBot.exe` as a per-user logon task. It opens the tray and starts the owned server; a Desktop Client has no server. `enable` registers future logon startup; launch `vBot.exe` or use `vbot server start` to start now. The task name is installation-owned, so custom task/service names are rejected. There is no Windows service. The bullets below describe source-checkout installations.

```bash
vbot autostart enable|disable|status [--task-name <name>] [--service-name <name>]
```

- `enable` registers OS autostart and starts the server now: a low-privilege per-user Windows Task Scheduler logon task (`--task-name`, default `vBot`) or a Linux systemd user unit (`--service-name`, default `vbot`) with login lingering.
- Windows Autostart uses the current user's interactive token and does not require an elevated terminal; run installation and lifecycle commands from a normal PowerShell so checkout, data, and server processes remain user-owned.
- `disable` removes the entry but leaves a running server untouched.

## Desktop

In a packaged installation, this launches the separate Desktop process and returns. A server-with-Desktop installation opens its recorded local server by default; a Desktop Client retains the last-used-server behavior. Explicit host/port options are forwarded. Closing Desktop leaves the server and tray running. The blocking behavior below applies to source-checkout launches.

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
