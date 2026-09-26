# vBot User Guide

This guide covers installing, configuring, using, operating, and integrating vBot. If you only want the shortest path to a running installation, start with the [README](README.md#get-started).

## Start here

A new installation takes three steps:

1. Run the standard Installer for your operating system.
2. Open `http://127.0.0.1:8420/` after the Installer reports that the server is running.
3. Use the setup guide to connect a Provider or OAuth subscription, choose a Model, and send the first message in the Session vBot created for you.

| I want to… | Go to |
|---|---|
| Install vBot or choose a Desktop/remote-client shape | [Installation](#installation) |
| Connect a Provider and start chatting | [First-run setup](#first-run-setup) |
| Understand Agents, Projects, and Sessions | [Agents, Projects, and Sessions](#agents-projects-and-sessions) |
| Reach an Agent through a messaging Channel | [Channels](#channels) |
| Schedule work | [Cron](#cron) and [Bootstrap](#bootstrap) |
| Diagnose or automate vBot | [CLI reference](#cli-reference), [Server API](#server-api), and [Operational notes](#operational-notes) |
| Develop vBot itself | [Development and verification](#development-and-verification) |

## Contents

- [Start here](#start-here)
- [Installation](#installation)
- [Requirements](#requirements)
- [Updating and uninstalling](#updating-and-uninstalling)
- [First-run setup](#first-run-setup)
- [Data directory and configuration](#data-directory-and-configuration)
- [Running the server](#running-the-server)
- [WebUI and Desktop](#webui-and-desktop)
- [Agents, Projects, and Sessions](#agents-projects-and-sessions)
- [Chat, Queue, and Built-in Commands](#chat-queue-and-built-in-commands)
- [Skills, Tools, and Sub-Agents](#skills-tools-and-sub-agents)
- [Settings and specialized Models](#settings-and-specialized-models)
- [Channels](#channels)
- [Cron](#cron)
- [Bootstrap](#bootstrap)
- [Extensions and Home Assistant](#extensions-and-home-assistant)
- [CLI reference](#cli-reference)
- [Server API](#server-api)
- [Development and verification](#development-and-verification)
- [Operational notes](#operational-notes)

## Installation

A standard installation is one command. Use the advanced options only when you want a non-default install directory, port, source track, Desktop shape, or Autostart policy.

### Fresh Windows install

Open a normal, non-elevated PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1 | iex
```

The Windows Installer downloads the matching native package, checks the release asset's SHA256 digest, and installs it for your user account. The application has its own `vBot.exe` tray and private Python runtime; it installs no Windows service and does not need a Git clone or Node.js. Server data remains separate at `~/.vbot`. Run it from a normal, non-elevated shell. The optional logon task belongs to your user account.

The three packages are Server, Server with Desktop, and Desktop Client. Desktop is independently opened from the tray or `vbot desktop`; closing it never stops the server. Open Desktop windows keep their version until reopened after an update. WebView may still use several Windows processes.

Fresh installs require a published matching Windows binary asset. If the selected release has none, the installer stops with an explanation; it does not silently clone the repository. On Windows, `-Dev` selects the native main installation: it prepares updates from a separate Git checkout and uses the same application lifecycle as release installations. Use `-SourceCheckout` for an explicit source installation; setup from an existing checkout without `-Dev` retains the source-development workflow.

### Fresh Debian-like Linux install

On Debian-like systems, including Raspberry Pi OS, run:

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash
```

The Installer can add missing prerequisites through `apt`, configures a systemd user unit, and starts the server. Browser automation is optional and uses the bundled `playwright-cli` Skill through the `bash` Tool. Install Node.js 18 or newer, npm, and `@playwright/cli` on the server host when needed; the Skill includes setup instructions. See [Browser automation](resources/skills/playwright-cli/SKILL.md).

After either standard installation, wait for the final summary to confirm that the server is running, then open `http://127.0.0.1:8420/`. The first-run setup is described in [First-run setup](#first-run-setup).

As with any `curl | bash` or `irm | iex` command, inspect [install.sh](scripts/install.sh) or [install.ps1](scripts/install.ps1) and run the downloaded file locally if you do not want to execute network content directly.

### Public Installer contract

The public entrypoints are `scripts/install.sh` for Linux and `scripts/install.ps1` for Windows. Fresh Windows application installs use the package described above. The following checkout-based contract applies to Linux and explicit Windows source installations: select a release, clone it into `~/vbot`, create `~/vbot/.venv`, obtain the matching WebUI, install vBot into that isolated environment, expose `vbot`, configure Autostart, and start the server. Runtime state remains separate under `~/.vbot`.

When the same Installer runs from inside a vBot checkout without an explicit installation directory or version, it installs that checkout into `<checkout>/.venv` and builds the WebUI locally. On Windows, explicit `-Dev` instead selects the native main installation. It never installs vBot into the system Python environment. The internal `scripts/setup.*` helpers configure a checkout only after the public Installer has established the checkout and environment; they are not end-user installation entrypoints.

### Install shapes

Choose an install shape by what you want to use:

| I want to… | Shape | Local server/WebUI | Desktop | Autostart |
|---|---|---|---|---|
| Run vBot and use it in a browser | Default server | Yes | No | Yes unless disabled |
| Run vBot and also use the Desktop app | Server + Desktop | Yes | Yes | Yes unless disabled |
| Connect this computer to an existing vBot server | Desktop Client | No | Yes | Never |
| Work on vBot itself | Development | Yes | Optional | Yes unless disabled |

`--desktop`/`-Desktop` adds Desktop to a full local server installation. `--desktop-client`/`-DesktopClient` installs only the CLI and Desktop clients for an existing remote server; it is mutually exclusive with local Desktop server mode. Windows Desktop Client can also follow main using `-Dev`; Linux keeps its separate development-mode restriction.

### Advanced Installer options

Linux options are passed after `bash -s --`:

| Option | Meaning |
|---|---|
| `--dir <path>` | Installation directory; default `~/vbot` or `$VBOT_DIR`, or the current checkout when invoked there |
| `--version <tag>` | Install a specific release tag; cannot be combined with `--dev` |
| `--dev` | Fresh install: track `main`; current checkout: add development dependencies; either path builds the WebUI locally and requires Node.js |
| `--data-dir <path>` | Runtime data directory; default `~/.vbot` |
| `--host <host>` | Server bind host; default `127.0.0.1` |
| `--port <port>` | Server port; default `8420`, or the existing Settings value when not explicitly overridden |
| `--desktop` | Add Desktop to a server installation |
| `--desktop-client` | Install only CLI and Desktop for an existing remote server |
| `--no-autostart` | Do not create or start the systemd user unit |
| `--skip-webui-build` | Require and reuse an existing `webui/dist`; release installs do this automatically after downloading the asset |
| `--service-name <name>` | Custom systemd user unit name without `.service`; default `vbot` |
| `--verbose` | Show technical setup output live; by default it is kept out of the terminal and retained in a temporary log only when attention is required |
| `-h`, `--help` | Show Installer help |

<details>
<summary>Linux examples</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --desktop
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --desktop-client
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --no-autostart --port 9000
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --dev
```

</details>

Windows accepts `-InstallDir`, `-Version`, `-Dev`, `-SourceCheckout`, `-DataDir`, `-HostName`, `-Port`, `-Desktop`, `-DesktopClient`, and `-NoAutostart`. Native packages default to `%LOCALAPPDATA%/Programs/vBot`; source installations default to `~/vbot`. `-SkipWebuiBuild` and custom `-TaskName` apply to the source installation path. The standard PowerShell `-Verbose` switch shows technical setup output live. Use the ScriptBlock form to pass options. `-AllowElevatedInstall` is an explicit escape hatch for disposable automation only; never use it for a persistent installation.

On Windows, `-Dev` is the native installation that follows `main`: it has the same
EXE, tray, update command and directory as a release installation. It keeps its
editable Git checkout under `<install>/source`, prepares a complete version before
startup and requires Git plus Node.js/npm for server assets. Native host source
changes additionally require LLVM and the Windows SDK. `-SourceCheckout` explicitly
selects the separate Python installation workflow.

An existing native installation can select its update source without moving:

```powershell
vbot application source main
vbot update
vbot application status
```

Use `application source main --from-checkout <path>` to retain an existing clean
branch checkout, or `application source release` followed by `vbot update` to select
published releases. Source selection preserves Git files. Main updates retain local
commits; uncommitted changes or divergent history require resolution before updating.
Failed preparation leaves the running version intact. A failed initial main update
leaves the complete base installation available for retry with `vbot update`.

<details>
<summary>Windows examples</summary>

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Desktop
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -DesktopClient
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -NoAutostart -Port 9000
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Dev
```

</details>

The public Installers show readable setup phases and elapsed time during long setup steps. Status labels and terminal colors distinguish work, success, warnings, and errors; set `NO_COLOR=1` to disable color. Technical setup output is written to a temporary log and discarded after a clean installation; when installation or verification needs attention, the log is preserved and its exact path is printed. The immediate Windows background server, Task Scheduler action, and optional `vBot Desktop` Start-menu entry use windowless launch paths; the Start-menu entry uses the bundled vBot icon. For server installations, both public Installers verify Autostart and server health before printing `vBot is ready`; a live URL appears only when the server is running. If Autostart registration fails, the application remains installed and the summary prints the exact normal-user recovery command.

### Install the current checkout

Run the public Installer from the repository root. It detects the checkout, creates or reuses `<checkout>/.venv`, builds the WebUI unless a matching `webui/dist` is explicitly reused, records `.vbot-install.json`, and installs the selected server or Desktop shape. This path is safe on PEP 668 systems because it never installs into the system interpreter.

Windows:

```powershell
.\scripts\install.ps1
.\scripts\install.ps1 -Desktop
.\scripts\install.ps1 -DesktopClient
.\scripts\install.ps1 -NoAutostart
```

Linux:

```bash
scripts/install.sh
scripts/install.sh --desktop
scripts/install.sh --desktop-client
scripts/install.sh --no-autostart
```

## Requirements

Native Windows packages include their Python runtime, dependencies and built WebUI. Normal installation and updates require neither Git nor Node.js. Preparing local application changes additionally requires Git and Node.js/npm on the machine.

For Linux and explicit Windows source installations, the Installer handles the application environment and attempts to install missing prerequisites through `winget` or `apt`:

- Python 3.11 or newer and Git are required, but the Installer provisions them where the supported package manager is available.
- Node.js and npm are required only for a development installation or a current-checkout WebUI build; release installs download a prebuilt WebUI.
- A Provider Connection and Model are required before an Agent can complete a Run, but you configure them after installation in the WebUI setup guide.

If automatic prerequisite installation is unavailable, the Installer stops with the exact missing dependency instead of partially configuring vBot.

## Updating and uninstalling

### Updating

Run the same command from your terminal or choose Update in the packaged Windows tray:

```bash
vbot update
```

For a packaged Windows application, a separate updater saves the operation and continues even if the calling terminal or vBot Run exits. A human CLI invocation normally waits for the final result, shows readable progress with elapsed time, and returns a failing exit code when the update fails. The final summary reports the version and verified outcome once; `--output plain` returns the structured operation record for scripts. Read-only `update status` reports the saved record. The tray displays the same saved state. No Agent is created for a human or tray update.

```bash
vbot update --detach           # return the saved operation id immediately
vbot update status            # inspect the latest saved operation
vbot update status OPERATION_ID
vbot update --no-restart       # prepare a candidate without changing the active version
vbot update activate OPERATION_ID
```

Official updates verify the signed package, preserve the selected shape and server target, prepare local changes and Extension dependencies, wait for accepted work to finish, stop the server, and create a data snapshot of every canonical database and the JSON configuration documents. They then verify the candidate before activating it and verify normal startup. If the candidate fails its verification start, the updater restores that snapshot, but only when the candidate changed the data and no other vBot server uses the data directory, and keeps the previous version once its startup is verified. After the candidate passed verification, the snapshot is never restored automatically; apart from this rollback, vBot restores a data snapshot on its own only to repair a damaged or missing database (see Data-store maintenance). If recovery cannot establish a safe result, the operation reports that it needs attention; when a data restore was interrupted, it names the `vbot data-store snapshot restore <snapshot-id> --all --yes` command that finishes it. An open Desktop window can continue using its previous version until reopened.

When called through Bash in a vBot Run, the command saves its operation before returning and automatically arranges a continuation in the same Session. The updater waits for the whole Tool batch to enter Session history, registers that continuation, and cancels and drains only the exact originating Run before draining other accepted work. The continuation checks the saved result; acceptance alone is not update success. Do not create an additional Bootstrap for this packaged update path.

### Updates in a source checkout

Close every vBot Desktop window on Windows before updating a source installation. The source updater reports each phase, including elapsed time during long steps. Its final summary distinguishes a verified server restart from a pending or skipped restart; failure details include recovery guidance. Output remains plain text when redirected, and `NO_COLOR=1` disables terminal color.

The updater preserves the recorded install shape, Python interpreter, dependency groups, source track, server target, and WebUI policy. Release installations move to the newest release with a matching WebUI asset; development installations update `main` and rebuild when needed. Before replacing current-format code, the updater creates and verifies a data snapshot of every canonical database and the JSON configuration documents; runtime data under `~/.vbot` or the configured data directory is not otherwise modified. A source update never restores that snapshot automatically. When the final server restart fails, the result names the snapshot and the previous revision: check out that revision, reinstall its dependencies, then run `vbot data-store snapshot restore <snapshot-id> --all --yes`, which loses everything written after the snapshot.

Use an explicit policy when the tracked checkout contains local changes or when the server should not restart. `--stash` and `--discard` apply only to source installations:

```bash
vbot update --stash       # reapply tracked local changes after updating
vbot update --discard     # permanently discard tracked local changes
vbot update --no-restart  # leave the server state unchanged
```

<details>
<summary>Windows source update recovery details</summary>

The Windows public Installer's `vbot` command invokes the recorded Python module instead of pip's replaceable `vbot.exe`, so the running command does not lock its own launcher during dependency updates. The updater refuses to change the checkout while that installation's exact `vbot-desktop.exe` is running and checks again immediately before pip changes the environment. A Desktop installation also refreshes its Installer-owned Start-menu shortcut.

An older Windows shim that still starts `vbot.exe` is refused before checkout mutation with a one-time source-based resume command; completing that recovery migrates the shim for future updates. If pip fails after a checkout advance, the failure output includes the same source-based resume command through the recorded Python executable, which remains usable even if pip removed the normal launcher.

</details>

### Uninstalling

Start the guided removal or reset flow:

```bash
vbot uninstall
```

The command shows the Installer-recorded target before offering three explicit scopes:

| Scope | Application | Runtime data |
|---|---|---|
| Application only | Removed | Preserved |
| Data only / reset | Preserved | Permanently deleted |
| Application and data | Removed | Permanently deleted |

Any data-removing scope displays the resolved directory and requires typing `DELETE`. A data-only reset preserves the previous server state: a running server restarts with fresh data, while a stopped server remains stopped. A systemd-owned server is controlled through its existing unit so Autostart ownership is preserved.

<details>
<summary>Non-interactive Uninstall commands</summary>

```bash
vbot uninstall --app-only --yes
vbot uninstall --data-only --yes
vbot uninstall --all --yes
```

Automation must choose exactly one scope and confirm it with `--yes`. A packaged application always uses its own recorded target and owned logon registration; conflicting lifecycle overrides are rejected. Source installations accept `--host`, `--port`, and `--data-dir` overrides, with custom Autostart names through `--task-name` on Windows or `--service-name` on Linux.

</details>

<details>
<summary>Uninstall safety and Desktop Client behavior</summary>

The command refuses protected roots, the home directory, any data target containing the application installation, a data target containing the caller's current directory, and application removal while the caller is inside the installation directory.

For a Desktop Client, `--app-only` and targetless `--all` remove only the local CLI/Desktop installation; they never resolve, probe, stop, or delete a default local server. A targetless `--data-only` is rejected because this install shape owns no server data. Deliberately resetting a server data directory from a Desktop Client requires `--host`, `--port`, and `--data-dir` together.

Application-removing scopes for server-owning installations first stop the exact selected server. If that stop fails, removal aborts and reports the preserved application directory. The bundled platform Uninstaller verifies the stop again before removing anything. A manifest-backed Desktop Client skips both server-stop stages unless explicit data removal selected a complete target.

A fresh managed installation is removed wholesale. An installation performed in an existing checkout removes the Installer-owned `.venv` and launcher but preserves the checkout. Desktop Start-menu or application-menu entries are removed only when the recorded install shape owns Desktop, so uninstalling a server-only shape preserves a Desktop entry owned by another installation.

On Windows, removal requests elevation and launches a helper that waits for the calling `vbot.exe` to exit before deleting its environment. Helper launch is not completed removal; the elevated window reports final completion or failure. Cancelling UAC leaves the installation and selected data in place, although a server stopped during preflight remains stopped. The underlying `scripts/uninstall.ps1` and `scripts/uninstall.sh` retain the same mandatory-stop contract for server-owning recovery and direct-setup entrypoints.

</details>

## First-run setup

Open `http://127.0.0.1:8420/`. vBot has already created an initial Identity Agent and its first Session, so you do not need to create an Agent before getting started.

1. Follow the setup guide to choose a Provider.
2. Connect an API key, an OAuth subscription, or a keyless local Connection such as Ollama.
3. Select the Agent's chat Model.
4. Return to Chat and send a message. A successful response confirms the complete path from the WebUI through vBot to the selected Provider and Model.

The WebUI is the recommended place to manage Connections and Accounts because it keeps credentials out of command history. A fresh server installation seeds global Agent defaults with thinking effort `high` and leaves temperature unset, so Agents request their Model's recommended temperature - or none at all, leaving the choice to Provider and API defaults - unless configured more specifically. Installation and updates preserve an existing `settings.json`, so these fresh-install defaults never replace an existing instance's choices.

<details>
<summary>Equivalent Provider CLI examples</summary>

```bash
vbot provider key set openrouter --stdin --refresh-models
vbot provider connect openai --connection openai:subscription
vbot provider usage --connection openai:subscription
vbot provider enable ollama
vbot model refresh openrouter
```

A key passed to `provider key set` may be retained by shell history. Prefer the WebUI, a protected environment variable, or a shell-specific history-safe workflow when entering a real secret.

</details>

A Provider describes an external model service and Adapter behavior. A Connection describes one authentication and endpoint mode under that Provider. An Account is a credential slot on a Connection; the default slot is named `default`, and additional named Accounts can coexist.

API keys resolve from the process environment first and `<data-dir>/.env` second. OAuth tokens live under `<data-dir>/oauth/`. A keyless local Connection such as Ollama still has enabled and reachability state even though it has no credential.

### Custom Providers

Settings can add a user-owned OpenAI-compatible endpoint without adding files under `resources/`. Use **Settings → Providers → Add custom** to set its stable id, display name, endpoint URL, authentication, optional Model discovery path, and manual Models. The initial Adapter choice is `openai_compatible`; each Custom Provider has one implicit `default` Connection. API keys are write-only and are stored as `VBOT_CUSTOM_<ID>_API_KEY` in the selected data directory's `.env`, never in `settings.json`.

The equivalent secret-free `settings.json` shape is:

```json
{
  "providers": {
    "custom": {
      "local-ai": {
        "name": "Local AI",
        "adapter": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth": "none",
        "models_endpoint": "/models",
        "defaults": {},
        "models": {
          "chat-model": {
            "name": "Chat Model",
            "context_window": 32768,
            "max_output_tokens": 4096,
            "capabilities": {
              "input_modalities": ["text"],
              "output_modalities": ["text"],
              "task_types": ["chat", "text_output"],
              "tools": true,
              "vision": false,
              "json_mode": false,
              "reasoning": false,
              "supported_parameters": [],
              "supported_voices": [],
              "task_options": {}
            }
          }
        }
      }
    }
  }
}
```

Use `"auth": "api_key"` for standard `Authorization: Bearer` authentication and enter the key through Settings or `provider custom save --api-key-stdin`. Omit or clear `models_endpoint` to use manual Models only. Discovery and manual Models can coexist: a manual record overrides discovered facts for the same wire id, while other discovered Models remain available. Saving through the WebUI/RPC reloads Providers and Models immediately; after direct file editing, validate with `vbot doctor settings` and restart vBot.

```bash
vbot provider custom save local-ai --name "Local AI" --base-url http://127.0.0.1:8080/v1 --auth none --models-endpoint /models --model chat-model
vbot provider custom list
vbot model refresh local-ai
vbot provider custom delete local-ai
```

`custom save` replaces the complete Custom Provider record; repeated `--model` flags create conservative chat Model entries. Use the WebUI for the full manual capability editor. Deleting a Custom Provider removes its generated data-directory API keys but deliberately keeps Agent/default/task Model references, which remain visible as unavailable until reconfigured.

## Data directory and configuration

The normal runtime data directory is `~/.vbot`. Select another target with `--data-dir` on server and RPC-backed CLI commands, or set `VBOT_DATA_DIR`. Run `vbot home` to print the absolute application and currently selected data directories; pass `--data-dir` to inspect an explicit target.

Setup, Runtime, direct `core/storage/layout.py` use, and managed Worktree creation all initialize this same non-destructive structure:

```text
<data-dir>/
├── artifacts/
│   ├── attachments/
│   ├── speech/
│   ├── models/
│   ├── debug/
│   ├── performance/
│   └── temp/
│       ├── atomic/
│       ├── bash/
│       ├── subagents/
│       ├── terminals/
│       └── web_fetch/
├── statistics/
├── agents/
├── archive/
├── bootstrap/
├── calendar/
├── channels/
├── cron/
├── extensions/
├── logs/
├── oauth/
├── processes/
├── projects/
├── prompts/
├── recall/
├── skills/
├── terminals/
├── .env
├── data-store.json
└── settings.json
```

`artifacts/attachments/`, `artifacts/speech/`, `artifacts/models/`, `artifacts/debug/`, and `artifacts/performance/` contain durable domain-owned artifacts; the performance folder keeps the newest 20 Recordings. The `artifacts/temp/` children remain separate: atomic replacement staging, 72-hour retained Bash output, Terminal output and Web Fetch snapshots, and 24-hour retained Sub-Agent activity. `statistics/` holds only the disposable local Statistics index, which is derived from Sessions; the automatic Provider usage history is the canonical `provider-usage.db` at the data root (see Data-store maintenance below). The tracked system Model DB remains `resources/models/`; only the complete instance runtime Model DB moves under `artifacts/models/`.

Independent roots keep their established ownership: `agents/` contains Identity Agent configs, default Workspaces, and private Skills; `projects/` contains Project metadata; `skills/` contains global user Skills; `bootstrap/`, `calendar/`, `channels/`, `cron/`, `extensions/`, `prompts/`, `recall/`, `terminals/`, `logs/`, `oauth/`, and `archive/` retain their existing records. Sessions of every Agent and Project live in `sessions.db`. Custom absolute Workspaces remain outside the data directory. `processes/` is reserved for future persistent process records and is not the Bash-output location.

Other entries appear at the data root when first needed:

- the canonical databases `sessions.db`, `channels.db`, `provider-usage.db` and `decisions.db`, and Extension databases next to other Extension state under `extension-data/<extension>/`; every database is registered in `data-store.json` (see Data-store maintenance below);
- `snapshots/`, `incidents/` and `quarantine/` for data snapshots and recovery, `data-store.lock`, and `data-maintenance.json` while an offline data operation is incomplete;
- `runtime/` with the control records of a running server, and `speech-engines/` after local speech setup;
- `pre-generation-1/` after converting an older data directory (see below).

The initializer copies `resources/data-dir/.env.example` only when `.env` is absent and creates a `settings.json` holding only `format_version` 1 when Settings is absent. It never rewrites either existing file; Setup separately retains ownership of fresh-install Settings defaults and explicit port updates. It writes `data-store.json` only into a data directory it has just created; an existing directory without it is refused (see Converting an existing data directory).

### Converting an existing data directory

Current vBot reads only persistence Generation 1: the canonical paths, databases registered in `data-store.json`, and JSON documents with a `format_version`. Setup and Runtime never convert or dual-read older data. A data directory written by a vBot release before Generation 1 (0.4.x) is converted once, offline, from a vBot checkout that includes Generation 1:

1. Stop vBot completely, including the desktop and tray application. The converter refuses while a server uses the data directory.
2. Back up the complete data directory with your normal filesystem backup mechanism.
3. Run a dry run and read its summary: counts per area, verification, and every skipped or approximated item. The data directory keeps its content.

   ```bash
   python -m scripts.converters.persistence_generation_1 <data-dir> --dry-run --report <file-outside-the-data-dir>
   ```

4. Install the conversion:

   ```bash
   python -m scripts.converters.persistence_generation_1 <data-dir> --report <file-outside-the-data-dir>
   ```

5. Start a vBot that includes Generation 1 (the old release refuses the converted directory), check Agents, Projects, Sessions and Channels, then create the first data snapshot:

   ```bash
   vbot data-store snapshot create --reason manual
   ```

The install moves every file it replaces or retires, including the old `sessions.db`, `session-store.json` and `session-snapshots/`, to `<data-dir>/pre-generation-1/` at the same relative path, together with `conversion-report.json`; nothing is deleted. vBot never reads that folder. It also holds old copies of credential files such as OAuth tokens, so treat it like the data directory. Delete it once the converted instance has worked for a while and a data snapshot exists. To go back before that, stop vBot, delete the files the report lists under `install.installed` and `data-store.json`, and move the content of `pre-generation-1/` back.

The converter refuses, changing nothing, while a server runs, when the directory is already converted, when `pre-generation-1/` already exists, when a source has an unsupported shape, or when the volume lacks the free space for staging. A failure before the install leaves the data directory unchanged. An interrupted install keeps vBot from starting on the directory; running the same command again finishes it.

### Data-store maintenance

The canonical databases, such as the Session database `<data-dir>/sessions.db` and the Provider usage history `<data-dir>/provider-usage.db`, are authorized by `<data-dir>/data-store.json`, which records each database's identity and format generation. FTS, Recall, Vector, Statistics, data snapshots under `snapshots/`, and quarantine bundles under `quarantine/` are derived or recovery data and never replace canonical history. Runtime refuses an existing root without a valid marker and never searches legacy files to guess how to initialize it. A damaged or missing canonical database is restored automatically from the newest verified data snapshot; the damaged files move to quarantine and a recovery incident under `incidents/` records the possible loss interval.

Inspect the canonical databases and their recovery state through the live server or the local offline commands:

```bash
vbot data-store status
vbot data-store snapshot list
vbot data-store snapshot create --reason manual
vbot data-store snapshot verify <snapshot-id>
vbot data-store incident acknowledge <incident-id>
vbot data-store snapshot restore <snapshot-id> --yes
vbot data-store snapshot restore <snapshot-id> --database sessions --yes
vbot data-store snapshot restore <snapshot-id> --documents --yes
vbot data-store snapshot restore <snapshot-id> --all --yes
vbot data-store unregister ext.<extension>.<name> --yes
```

`status` reports safe operational metadata per database, including the Session search index state, verified data snapshots, and every unacknowledged recovery incident without returning Session content; for a stopped local server it reads the data directory directly. Snapshot creation is an explicit backup, through the running server, of every canonical database and of the JSON configuration documents (settings, Agents, Projects, Channels, prompt layouts, Cron, Bootstrap and Calendar jobs, the Skill policy, Terminal state, MCP connections and OAuth tokens; attachment and speech metadata stay out, like the files they describe). A recovery incident remains visible until the exact incident is acknowledged; acknowledgement does not delete snapshots or quarantine evidence.

Restore is offline maintenance: it requires `--yes`, checks the snapshot first, stops the exact target server when it runs and starts it again afterwards, and must be rehearsed on a copied data directory first. Without a selector it restores every database in the snapshot; `--database` restores only the named ones; `--documents` restores the JSON documents as one set, alone or together with `--database`; `--all` restores the complete snapshot, moves databases registered after the snapshot to quarantine, and takes no other selector. Restored documents become exactly the snapshot's: documents created after it are removed, and every replaced or removed document is kept under `quarantine/json-documents/`. An interrupted restore keeps the server from starting until a restore is repeated and completes.

`unregister` releases the database of a removed Extension. While it stays registered, data snapshots keep copying it, and once its file is gone, snapshot creation and updates refuse until it is released. The files move to `quarantine/` and the registration is dropped. It accepts only Extension databases (`ext.<extension>.<name>`), is refused while an Extension has the database open, and requires `--yes`. Earlier snapshots keep their copy, and restoring that database from one registers it again.

The Session database stores Runs, Messages, Tool invocations/results and checkpoints relationally.

Desktop owns separate per-user settings because it can connect to different servers. On Windows they live under `%APPDATA%\vbot`; on Linux they live under `$XDG_CONFIG_HOME/vbot` or `~/.config/vbot`. Remembered servers, wakeword configuration, and imported wakeword Models are Desktop-local and are not server Settings.

Use the WebUI Settings view or the cataloged CLI Settings paths for validated changes:

```bash
vbot config list
vbot config describe server.port
vbot config set server.port 9000
vbot config patch --set web_search.provider searxng --set web_search.searxng.base_url https://searxng.example/
vbot config raw
vbot doctor settings
vbot doctor config
```

Bare `vbot config` is equivalent to `config list`; `config get`, `set`, `unset`, and atomic multi-operation `patch` use public paths rather than internal `settings.json` keys. `config effective` shows the normalized public document, while `config raw` is diagnostic only. `doctor settings` strictly validates the target `settings.json`; `doctor config` checks every JSON document vBot keeps in the data directory, from Agent, Project and Channel files to OAuth tokens and attachment metadata. At runtime, malformed root Settings fall back safely, invalid top-level Settings sections are omitted while valid siblings remain active, and invalid individual Agent or Project records are skipped. The source file is not silently rewritten, and mutations that could overwrite invalid source state are blocked until it is repaired.

## Running the server

Run in the foreground from a checkout:

```bash
python server/main.py
```

Use the installed lifecycle commands for a managed background process:

```bash
vbot server start
vbot server status
vbot server restart
vbot server stop
```

Override the target at the leaf command:

```bash
vbot server start --host 127.0.0.1 --port 9000 --data-dir ~/.vbot-alt
```

Manage OS autostart separately when needed:

```bash
vbot autostart enable
vbot autostart status
vbot autostart disable
```

The default host is `127.0.0.1` and the default port is `8420`. Confirm the exact vBot health contract with:

```bash
curl http://127.0.0.1:8420/health
```

The expected body is exactly `{"status":"ok"}`.

## WebUI and Desktop

The WebUI at `http://127.0.0.1:8420/` provides Chat, Agent and Project management, Cron, System Prompt editing, Settings, Logs, Statistics, and Debug views. It connects to the server-owned runtime, so closing the browser does not stop a Run.

Launch the optional Desktop accessor with:

```bash
vbot desktop
vbot desktop --host 192.168.1.50 --port 8420
```

On Windows, `-Desktop` and `-DesktopClient` installations also create a `vBot Desktop` Start-menu entry that launches `vBot.exe desktop` without a console window. Explicit source installations use the windowless `vbot-desktop` GUI launcher. `vbot desktop` remains the equivalent command for terminal use.

Without explicit host and port, Desktop opens its Connection screen and auto-connects only when a remembered last-used server exists. It does not silently assume localhost. Probe failures return to the same screen with the target prefilled, and the native Server menu can switch or reconnect at runtime.

Desktop loads the same server-served WebUI. A local folder picker would browse the client machine rather than a remote server, so Project paths remain server-side paths entered through the same WebUI field.

Desktop Voice runs wakeword detection and microphone capture locally. Up to eight built-in or imported pyopen-wakeword TFLite Models (wake phrases) can listen at the same time. Each phrase has its own action per server under **When heard** in Settings → Voice: **Send a command** records what you say next and sends it to the phrase's Agent or the server's **Default Agent**; **Start or end Live voice** and **Start Live voice** control a Live voice call instead. Hey Nabu and Okay Nabu react to each other, so give them the same action. Imported Models are validated and stored on the Desktop machine; training happens outside vBot. Nothing is uploaded unless a command phrase matches. After a match, the command recording, including about 0.3 s of locally buffered audio immediately before detection, is sent to the active server's speech transcription endpoint; a recording ends after 1 s of silence and after 2 minutes at the latest.

**Echo cancellation** (Windows, on by default) removes what the computer plays through its default output device from the microphone signal, so Chat text-to-speech, Live voice replies, or other audio from loudspeakers do not trigger wake phrases and disturb command recordings less. It needs no setup; with headphones it has nothing to remove and stays harmless. For loudspeaker use, place the microphone near you: a headset microphone lying on the desk, especially one with built-in noise suppression, hears too little of your voice and distorts the echo too much for wake phrases to work reliably.

## Agents, Projects, and Sessions

### Live voice companion (preview)

Live voice can operate Chat and coding-agent Terminals while you speak. Choose its Model under **Settings -> Voice -> Live voice**; once a Model is chosen, a compact control appears at the bottom of the Main menu, above the microphone and connection indicators. It runs on OpenAI GPT-Live (an OpenAI API key or the ChatGPT subscription login under **Settings -> Providers**) or on xAI Grok Voice (an xAI API key or SuperGrok login). Then choose **Start Live** and allow microphone access. The same button becomes **Stop Live** while connected. Clearing the Live voice Model hides the control and ends an active voice connection. GPT-Live always hands app work to a backend Model of the same Provider (default `gpt-5.6-terra`); choose that Model and its **Backend reasoning** effort in the Live voice settings. Grok Voice operates vBot itself by default; pick a Grok backend Model in its settings to delegate app work instead. Grok Voice audio passes through the vBot server, which relays it to xAI. No separate vBot Agent or speech Task Model needs configuring.

Use a browser over **localhost or HTTPS**. The Desktop app also works with remote plain-HTTP servers it knew when it started; after adding a new server, restart the Desktop app before using the microphone with it. In the Desktop app, wake phrases keep working during a Live call: a command phrase pauses the call while your command records, and the voice Model is told to ignore speech that starts with a wake phrase. A wake phrase can also start or end Live voice (its **When heard** action), and on Windows an optional global shortcut (**Settings -> Voice -> Live voice shortcut**) starts and stops it. The voice connection remains active across Chat/Terminals navigation; **Stop Live** closes it. Providers bill connected voice time, including idle time, separately from backend Model usage.

Example requests: "Open Terminals and start four Codex terminals in /home/me/project", "Maximize the second terminal", "Put the fourth terminal first", or "Open Joel's chat and tell him to continue". Codex and Claude Code must already be installed and authenticated **on the vBot server**; working directories refer to that server. The companion can start the requested number of coding terminals (subject to the existing server capacity), read and send terminal input, select/reorder tiles, and maximize/restore the existing layout. It can close individual terminals and create, select, rename, or delete user and Agent groups. Closing stops the terminal and removes its tile; deleting a group stops every running terminal in it. It uses exact Session targets for chat messages and preserves normal Queue behavior.

While listening, it receives new vBot Run completion/failure updates, can relay Agent questions, and can summarize replies on request. It does not infer completion from quiet terminal output. Ending voice does not stop coding terminals or vBot Runs. Errors use the usual app notifications; sent chat messages remain in their normal Sessions.

The integration has offline protocol/action tests and a keyless browser check. Real GPT-Live speech, account access, and Model-selected actions remain unverified until an eligible API key is available. An uncertain operation is not automatically repeated; inspect the destination before asking again.

### Identity Agents

An Identity Agent is durable personal identity under `<data-dir>/agents/<agent-id>/`. It owns a Workspace containing `SOUL.md`, `USER.md`, and `MEMORY.md`, a `memory` Tool, private Skills, permissions, and Sessions. A custom absolute Workspace is allowed; relative file and shell Tools use the active working directory, which is a separate concept.

Create and inspect Identity Agents through the WebUI or CLI:

```bash
vbot agent list
vbot agent create coder "Coding Agent" --model openrouter/anthropic/claude-sonnet-4
vbot agent show coder
vbot agent update coder --thinking-effort high
vbot agent rename coder researcher
```

`agent update --project <project-id>` selects the Project used for relative file and shell work without moving the Agent's Workspace or Memory; `--clear-project` removes that selection. Workspace relocation is a separate operation: use `--workspace <absolute-path>` or `--default-workspace`, optionally with `--copy-workspace-files` to copy `SOUL.md`, `USER.md`, and `MEMORY.md` into the destination.

Agent create/update also expose delegation policy through `--subagent-allow <agent> ...` and Agent Policy through `--compaction-policy <json-object>` / `--clear-compaction-policy`. `--clear-model` and `--clear-fallback-models` restore global-default inheritance. Create/update output includes the saved id, Workspace, selected Project, effective Model, effective Policy, and provenance; Workspace moves also report copied and backed-up files. Creating an Agent without an Agent or global-default Model is allowed for onboarding, but the result explicitly warns that it cannot run and points to `vbot model list --task chat` followed by `agent update --model`.

`agent rename <current-id> <new-id>` moves the complete Identity Agent tree, including Sessions, the internal Workspace, Memory, prompts, and private Skills. Live Channel targets, non-terminal Cron jobs, bare Identity Agent delegation entries, and functional Sub-Agent parent links are retargeted; historical provenance remains unchanged. The operation refuses collisions and runs while either id is busy, and rolls back server-owned changes if a reference cannot be updated.

### Projects and Project Agents

A Project registers a server-side repository path, one Source Format, optional auto-load files, Project defaults, and Sessions. Its Source Format is either OpenCode (`.opencode/agents/` and `.opencode/skills/`) or Claude Code (`.claude/agents/` and `.claude/skills/`). Exactly one format is active; vBot does not merge them.

Project Agents are Config Agents scanned from the Project Team. They are profiles rather than identities: they have no Workspace, private Memory, private Skills, or `memory` Tool. Their runtime configuration resolves from repository definitions, Project defaults and capability ceilings, and vBot-owned overrides. Their Sessions live under the Project anchor and their address is `agent@project`.

```bash
vbot project add ./my-repo --name "My Project" --format opencode
vbot agent update coder --project my-project
vbot project list
vbot project show my-project
vbot project set my-project --default-agent orchestrator
vbot project override set my-project orchestrator temperature 0.3
vbot session create orchestrator@my-project
```

Project registration and Team scanning never write to the repository. Normal Agent Tools may write there during a Run when the Project is the working directory.

Project `add`/`set` can replace the Tool Whitelist and bundled/global/Project Skill policy lists. `override set`/`override clear` manage one Project Agent's vBot-owned model, temperature, thinking-effort, or Compaction Policy tier without editing the repository. `project remove --copy-rooted-agent-files` preserves `SOUL.md`, `USER.md`, and `MEMORY.md` before rooted Identity Agents with custom Workspaces are reset to their default Workspace; removal output lists every affected Agent and file effect.

### Rooted Agents and Project Context Loading

A Rooted Agent is an Identity Agent whose saved Project selection points at a registered Project. It keeps its bare address, identity-owned Session storage, Workspace, Memory, private Skills, and permissions, while relative file and shell work and Project context use the selected repository.

An Identity Agent with the `project` Tool sees the registered Projects in its System Prompt. Before working in a registered Project that is not its current working Project, it calls `project` with the Project id and waits for the result. The Tool returns the Project's current auto-load files, absolute cwd, and Project Skills; it does not change the Agent's cwd, Rooting, Workspace, Session owner, Skill scope, permissions, or identity. Subsequent file work therefore uses absolute paths. Every `bash` call that should run in the Project sets `workdir` to the returned cwd again: `bash` starts a new one-shot shell for each call, so `cd` and other shell-state changes never persist into the next call.

### Sessions

Sessions are explicit, append-only conversation histories. Server and product paths create the Session before sending Chat content. Runs, Tool events, Usage, Compaction checkpoints, titles, Channel metadata, and Continuation state persist with the Session.

```bash
vbot session list coder
vbot session create coder --make-current
vbot session fork coder SESSION_ID --target-agent reviewer
vbot session rename coder SESSION_ID --title "Research notes"
vbot session policy set coder SESSION_ID --policy '{"enabled":false,"trigger":{"type":"context_ratio","threshold":0.8},"strategy":{"type":"summary_tail","tail_tokens":15000,"summary_model":null}}'
vbot session delete coder SESSION_ID --yes
```

`session list` returns at most 100 Sessions by default. Use `--limit`, pass the returned JSON as `--cursor`, or explicitly request `--all` to collect every page.

Deleting an Agent, Project, or Session archives its vBot-owned state rather than silently erasing it. Project source repositories are never archived or removed.

## Chat, Queue, and Built-in Commands

One Session admits one active Run. New messages sent while it is busy enter its Queue and execute in order; WebUI and Channels surface queued state. Cancellation is cooperative and stops further model or Tool progression as quickly as possible, but already-running external work may not be hard-abortable.

The composer accepts plain text, uploaded attachments, `@` file mentions, Built-in Commands, and Skill triggers. Image, audio, video, and text/file inputs are normalized into content blocks; Provider adapters receive only formats they support. Audio transcription is cached on the attachment after its first successful transcription.

Built-in Commands are owned by Chat and work in the WebUI; Channels support the same set except `/agent`:

| Command | Behavior |
|---|---|
| `/help` | Show the current command catalog |
| `/status` | Show Agent, Session, Model, Run, and Queue status |
| `/stop` | Cancel the active Run for this Session |
| `/new` | Create and move to a new Session after the current Run finishes |
| `/rename [title]` | Set the Session title; no title clears it |
| `/model` | Show the effective Model; `/model <value>` sets it and `/model reset` clears the override |
| `/agent` | Show available personal and Project Team Agents |
| `/agent <address> [task]` | Move the whole Session to another Agent and optionally start a takeover task; unavailable through Channels |
| `/handoff [agent:<address>] [instruction]` | Create a fresh target Session with an Agent-generated handoff and start the receiving Agent |
| `/learn [request]` | Ask an Identity Agent to author or improve a Skill |
| `/reflect [focus]` | Fork and review an Identity Agent Session through the Reflection policy |
| `/compact [instruction]` | Manually create a Compaction checkpoint while no Run is active |

`/agent` changes Session ownership; `/handoff` creates a new target Session and leaves the source intact. Through a Channel, `/handoff` relays the target's first response once but does not change the Channel's configured Agent or future routing.

## Skills, Tools, and Sub-Agents

Skills are instruction packages loaded from bundled resources, `<data-dir>/skills`, the active Project's Source Format directory, trusted Extensions, and an Identity Agent's private `skills/` directory. The effective catalog also respects Agent and Project allowlists and Skill requirements.

An Agent can load a Skill through the `skill` Tool. Users can explicitly trigger a load with `/skill-name` or `$skill-name`; the original user message remains part of the request. The catalog text is pinned when a Session first builds its prompt, while activation remains live: a newly authored and allowed Skill can be triggered immediately and is announced to an existing Session without rewriting its pinned prompt prefix.

The WebUI and RPC Skill manager can author global and private Identity Agent Skills. Project Skills remain repository-owned and are edited through normal file Tools. Bundled Skills are read-only. `/learn` is an Identity-Agent authoring workflow over the same validated Skill core.

In **Configure -> Skills**, use the **+** beside search to install a Skill. Paste a link or upload a `.skill`, ZIP or TAR archive from your computer, choose global or private Agent storage, and check the source. If it contains several Skills, select one. The preview shows the destination and any existing copy; replacing a different package requires an explicit choice. After installation the manager opens the saved Skill. The same dialog links to folder setup and custom Skill creation.

The CLI also installs a complete Skill from a directory, `.skill`/ZIP/TAR archive, archive-download URL, or public GitHub repository/Skill directory:

```bash
vbot skill install ./research.skill --scope agent:coder
vbot skill install https://github.com/owner/repo/tree/main/skills/research --scope global
vbot skill install https://github.com/owner/repo --scope global --dry-run
```

Use `--path <directory>` when a source contains multiple Skills. `--dry-run` validates without writing; an identical package is unchanged, while replacing different files requires `--replace --yes` and removes local modifications too. Public skills.sh and ClawHub Skill links are also supported; other catalogs can supply their repository or archive-download link. Installation preserves supporting files and binary assets without running scripts or installing dependencies. Local paths refer to the server machine; a remote CLI does not upload client files.

An Identity Agent can follow the bundled `vbot-cli` Skill and install for itself with `--scope own`, including while a Project is loaded. Outside a Run, use `agent:<id>` or `global` explicitly. Private Skills become available to their owner subject to requirements and disable policy; global installs retain the Agent's existing Skill selection. Installation does not grant Tools or credentials. Use `skill inventory` to inspect saved packages and the Agent's `skill` Tool to verify its effective catalog. Uninstall with `skill delete <name> --scope global|agent:<id> --yes`.

Tools are runtime capabilities exposed according to Agent, Project, Extension, and Settings policy. Inspect the public catalog and one Agent's complete System Prompt with:

```bash
vbot tool list
vbot skill list
vbot skill read SKILL_NAME --scope global
vbot skill create librarian --scope agent:coder --file SKILL.md
vbot prompt preview coder
```

The CLI Skill manager authors only global and private Identity Agent scopes; `inventory` exposes package ids and editable scopes; `inspect <inventory-id>` reads the complete original Skill even for read-only sources. It supports `install`, `read`, `create`, `update`, `delete`, `file write`, and `file remove`. Project Skills stay repository-owned and bundled Skills stay read-only. The Prompt manager likewise supports default and `agent:<id>` scopes, custom user blocks, and complete layout order/enabled-state updates. Use `prompt show <block-id>` to read the full content before an update.

The `subagent` Tool delegates a bounded task to an authorized Identity or Project Agent in a child Session. Identity Agents may be allowed to target all Agents or an explicit list; Project Agents remain confined to their own Team. Foreground work returns directly, while top-level background work completes asynchronously and wakes the parent with the finished results. Nested Sub-Agents run in the foreground, and background Bash is unavailable inside a Sub-Agent so work cannot be stranded after the child Session ends.

Default limits are four levels of nesting, eight Sub-Agents per model turn, and a 60-minute foreground timeout. These are configurable in Settings. Each admitted child Run gets a supplemental activity file under `<data-dir>/artifacts/temp/subagents/`, retained for 24 hours after completion; canonical child history remains in its Session.

## Settings and specialized Models

Settings centralizes validated runtime policy. Major areas include:

- Agent defaults for Model, fallback Model, temperature, thinking effort, Tools, and Skills
- Sub-Agent authorization and depth, per-turn, and timeout limits
- Reflection cadence and review behavior for Identity Agents
- Compaction strategy, trigger, Model selection, and Agent, Project, or Session overrides
- Recall backend and semantic search configuration
- Web Search Provider configuration
- Specialized Models for speech, embeddings, and images
- Provider Connections, Accounts, credentials, enabled state, and local reachability
- Channels and denied-chat discovery
- trusted Extensions and Extension settings
- Session title generation, local-model context, Appearance, Logs, and Debug behavior
- Desktop-local Voice settings when running inside Desktop

For an Agent's primary chat Model, list the currently selectable catalog through the running server:

```bash
vbot model list --task chat
vbot model list --provider openai --task chat --capability tools
```

The result contains only Models with at least one usable Connection (including enabled keyless Connections) and prints the exact id accepted by `agent create --model` or `agent update --model`. It also includes the effective context window, capabilities/task types, and `reachable: no` for a local Model whose service is currently down. Additional repeatable filters are `--capability`, `--task`, `--input-modality`, and `--output-modality`; `--min-context-window` applies a token floor.

### Recall

Recall searches canonical persisted Session history; it does not replace curated `MEMORY.md`. Available first-party backends are `canonical_scan` for direct chronological scanning, `sqlite_fts` for indexed substring and relevance search, `vector` for semantic Passage search through the configured `text_embedding` Model, and `hybrid` for fused lexical and semantic results. SQLite indexes under `<data-dir>/recall/` are derived and disposable; incompatible schemas or embedding spaces rebuild rather than migrate.

The `session_search` Tool exposes Recall to Agents. The `history` Tool is Session-scoped and becomes available when Compaction has moved earlier detail behind a checkpoint.

### Compaction and Continuation

Compaction appends a checkpoint Projection and never rewrites or deletes older Session records. Automatic Compaction runs only at safe completed Model boundaries according to the effective Policy; `/compact` invokes the selected strategy manually when no Run is active. Older detail remains discoverable through `history` and Recall.

When a visible Run is interrupted, Continuation retains a private checkpoint regardless of whether the cause was user Cancel, a Provider or network failure, a timeout, a process restart, or an internal failure. The WebUI exposes no checkpoint banner or recovery controls. The next normal user message receives the checkpoint automatically alongside the new instruction, and a complete response resolves it.

### Specialized Models

Specialized bindings keep non-chat tasks independent from the Agent's primary Model:

| Task type | Used for |
|---|---|
| `speech_to_text` | WebUI microphone input, audio attachments, and Desktop Voice transcription |
| `text_to_speech` | Agent speech output and the speech synthesis endpoint |
| `text_embedding` | semantic and hybrid Recall |
| `decision` | structured judgments through the evaluate Tool and the Jev workspace |
| `image_generation` | image generation and editing, including source-image workflows when the target supports them |

The `image_generation` Tool writes generated files into a caller-owned `image-gen/` directory. Identity Agents always use `<Workspace>/image-gen/`, including when Rooted in a Project; Project Config Agents use `<Project cwd>/image-gen/`. The Tool returns the absolute local paths, and Chat exposes referenced files through signed `/api/files/` URLs without keeping a second image copy in the data directory.

Use Settings for target-specific option forms, or inspect and bind them through the CLI:

```bash
vbot task-model list
vbot task-model target list text_embedding
vbot task-model option list image_generation openai/gpt-image-1::api-key
vbot task-model set text_embedding openai/text-embedding-3-small::api-key
vbot task-model clear text_embedding
```

### Jev decisions and application control

Configure an OpenRouter key, then select **Decision** in **Settings → Specialized Models**. Available targets include `typesafe/jev-1.13` and `~typesafe/jev-latest`; the latter follows upstream updates. If targets are missing, refresh the Model DB after configuring the key.

Open **Jev** from the main navigation. Create a saved experiment or start from Support triage / Task requirements. Supply text or JSON and add focused questions: **Choice** selects a named option, **Score** rates ordered levels starting at zero, and **Noul** estimates yes on a zero-to-one scale. Explicit criteria help clarify meanings. Evaluate, inspect distributions/model/usage, compare history entries, or reuse a previous input. Every evaluation retains its own input and target. Confidence measures concentration of answers, not correctness; uncertain or missing evidence may produce intermediate values. This is not a deterministic field validator or a validated automatic LLM router.

Experiments open in a single workspace with **Setup** and **Results** tabs. Existing evaluations open on Results. Each result shows a short preview of the state used for that evaluation; **View full state** opens the complete text or JSON in a scrollable reader with Copy. Comparing results keeps each state's preview beside its own answers. Question identifiers are managed automatically. Setup edits autosave; the optional Save action sits after the fields.

Agents with access to the `evaluate` Tool use the same configured Model. Their ordinary Chat Model remains independent. Jev currently accepts text/JSON, not screenshots or image attachments.

For **Application control**, provide a goal and a state-reading command. Commands run on the **vBot host**, with an executable, one literal argument per line, and an absolute working directory. The observation command must print UTF-8 JSON with exactly these fields:

```json
{"state": {"current": 2, "target": 5}, "done": false}
```

Define at least two named Actions with descriptions and fixed commands; a no-op Action can leave the application unchanged. Jev selects an Action id, and vBot executes only the command you assigned. A Python script, an application's CLI, or an adapter contacting another machine can implement the commands. The application owns its state, permitted behavior and input handling. Jev does not generate command lines, and vBot contains no built-in game.

Start control to repeat observation, decision and action. Switching tabs or closing the browser leaves it running; return to inspect progress or explicitly cancel. The configured delay is additional to command and Model latency. A maximum of zero means continue until stopped, `done: true`, or an error. Errors stop execution without automatically replaying actions. Server interruption does not resume a control automatically. An interrupted Action may already have taken effect; a new start always reads fresh state. History retains the latest steps and the cumulative completed count.

### Local speech recognition

Qwen3 ASR, Parakeet TDT v3 and Nemotron 3.5 ASR run on the **vBot server machine**, including when
the WebUI or Desktop connects from another computer. They require no paid API
or subscription. In **Settings -> Tools & Media -> Specialized Models**, select
a local speech-to-text engine and choose **Install**. Setup runs on the server
and continues if you leave Settings. Its status shows environment checks,
downloads, installation and verification; a failed setup offers **Try again**.
Once verification succeeds, choose **Restart server**. This interrupts active
Runs, reconnects the interface and checks local speech availability again.

In a packaged Windows installation, setup creates a managed speech environment
under the data directory and runs speech in a child worker; it never installs
packages into the released runtime. In a source installation, setup uses the
server's Python environment and the shipped `local-speech` extra, without replacing
the running Desktop launchers. It preserves a working compatible
PyTorch installation, prepares CUDA 12.8 support for a detected NVIDIA GPU, or
uses the platform's CPU/Apple build. NVIDIA drivers must support that build;
verification runs a small GPU calculation before offering restart. Other GPU
platforms can use a manually installed [PyTorch build](https://pytorch.org/get-started/locally/).
`Automatic` uses CUDA/ROCm when available, then Apple MPS,
otherwise CPU. CPU execution is available but can be slow. This extra is separate
from the normal server and Desktop dependencies; it does not install NeMo, vLLM,
or the separate `qwen-asr` package. All three engines use native Transformers adapters.

In **Settings → Tools & Media → Specialized Models → Speech to text**, select
**Qwen3 ASR (local)**, **Parakeet TDT v3 (local)**, or
**Nemotron 3.5 ASR Streaming 0.6B (local)**; searching for **local** finds
all three. Expand its options to choose device, precision,
or a model directory. Qwen defaults to the 1.7B model, also offers 0.6B, and accepts
an optional language and vocabulary/context hint. Parakeet detects language
automatically. Nemotron accepts an empty language for automatic detection or a
code such as `de` / `de-DE` for German. Its native streaming engine reuses
computed context inside each recording segment; the current Chat still returns
the complete transcript after submission. You can also select an engine through the CLI:

```bash
vbot task-model set speech_to_text local/qwen3-asr
vbot task-model set speech_to_text local/parakeet
vbot task-model set speech_to_text local/nemotron3.5-asr
```

The first non-silent transcription downloads the selected public checkpoint from
Hugging Face and loads it. This can take several minutes and needs disk space for
model weights. Chat shows live download, model-loading and transcription phases
with elapsed time above the composer and in the microphone tooltip. A cached
model skips downloads; an already loaded model skips loading. Failures release
the microphone for another attempt. The server's standard Hugging Face cache is reused (`HF_HOME` can
relocate it). Complete model files are reused automatically without online update
checks, including after unloading or restarting. Only missing files are downloaded;
interrupted downloads resume their existing model revision. Alternatively, point
**Model directory** at a complete compatible Transformers checkpoint on the server.
Recordings are processed by the local engine, without a transcription API call.

Local speech models stay loaded independently, so STT and TTS can remain ready
at the same time. In **Specialized Models → Local speech memory**, each loaded
model has its own **Unload from memory** button. Unloading STT leaves TTS loaded,
even while TTS is generating audio. A model's button is disabled while that model
is busy. Downloaded files stay on disk; the next use loads that model again.
Changing an engine's model/device options replaces only its own cached model.
Shutdown releases all models; inference failure releases only the failed engine.
Loading and inference run outside the server Event Loop; cancelling a request
waits for already-started inference to finish safely.

All existing microphone and audio-attachment paths use the selected engine.
Long recordings are split into segments of at most 30 seconds, preferring a quiet
boundary and preserving every audio sample. Returned segment times describe these
audio chunks, not word-level alignment. The current interface returns a completed
transcript; it does not stream partial text or distinguish speakers. Desktop Voice
allows up to ten minutes for a transcription response, including a first download.

The pretrained Models are [Qwen3-ASR-1.7B-hf](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf),
[Qwen3-ASR-0.6B-hf](https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf) (Apache-2.0),
[NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
(CC-BY-4.0), and [NVIDIA Nemotron 3.5 ASR Streaming 0.6B](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)
(OpenMDW-1.1). See [Third-party notices](THIRD_PARTY_NOTICES.md#local-speech-models).

### Local speech synthesis

In **Settings -> Tools & Media -> Specialized Models -> Text to speech**, search
for **local** and select **Qwen3-TTS (local)** or **Chatterbox Multilingual V3 (local)**.
Choose **Install** for that engine. Setup continues across navigation, reports its
phase and offers retry on failure. TTS becomes available immediately after
verification; its isolated environment does not require a server restart.

Both engines support German and require no paid API. Qwen3-TTS offers nine preset
voices, automatic or explicit language selection, and style instructions on its
1.7B CustomVoice model; the smaller 0.6B CustomVoice model is also selectable.
Chatterbox explicitly uses the multilingual **V3** checkpoint, with language,
expressiveness and guidance controls and the upstream built-in voice/watermark.
No voice-cloning input is exposed by this integration.

After saving the binding and options, enter a short text and choose **Generate
voice preview**. The first request downloads weights into the Hugging Face cache,
then loads the model and generates audio. Live status and elapsed time remain
visible; the audio player appears when the complete WAV is ready. The Agent's
existing `text_to_speech` Tool uses the same saved engine and voice options.
Local requests accept up to 5,000 characters and split longer passages within
that limit at sentence/word boundaries. STT and TTS models stay loaded independently;
each model has its own **Unload from memory** button.

The optional `local-tts` extra installs uv, which prepares managed Python 3.12
and separate SDK environments under `<data-dir>/speech-engines/`. Fixed recipes
in `pyproject.toml` install Qwen's SDK and a pinned official Chatterbox revision
containing V3 (the PyPI 0.1.7 source predates V3). The server's STT packages are
not downgraded. Setup installs matching Torch/torchaudio builds and verifies
imports and, on NVIDIA systems, GPU execution. Chatterbox currently uses upstream's
Torch 2.6 / CUDA 12.6 combination; GPUs requiring a newer Torch are not supported
by that recipe. Qwen uses Torch 2.11 / CUDA 12.8. CPU and Apple builds are selected
on systems without a detected NVIDIA GPU. First downloads require internet access;
Downloaded models are reused automatically without online update checks. Only
missing files require internet access; there is no offline switch to configure.

Model sources and licenses: [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
(Apache-2.0) and [Chatterbox](https://github.com/resemble-ai/chatterbox) (MIT).
See [Third-party notices](THIRD_PARTY_NOTICES.md#local-speech-models).

### Web page reading and extraction services

The `web_fetch` Tool reads public pages, extracts document text, and shows image URLs to vision-capable Models. Long pages arrive as compact excerpts with saved references for further reading and searching. Those follow-up reads do not fetch again. References stay available to the same Agent in the same Session for 72 hours.

Under **Settings → Tools & Media → Web Fetch**, choose **Direct (no service)** or opt into Firecrawl, Tavily, Exa, or Parallel. Direct fetching is the default and requires no extraction-service account. Optional services can improve results on blocked or JavaScript-heavy pages:

- **Only when direct fetch fails** sends failed, blocked or unreadable pages to the selected service.
- **Prefer this service** uses it first for page URLs and tries direct fetching if it fails.
- Set the displayed API-key variable in the `.env` file in the vBot data directory. Firecrawl, Tavily and Exa share their keys with Web Search; Parallel uses `PARALLEL_API_KEY`. Key presence alone does not enable a service.

The selected service receives requested URLs and may charge for extraction. Free allowances, paid rates and rendering capabilities vary; check [Firecrawl pricing](https://www.firecrawl.dev/pricing), [Tavily credits](https://docs.tavily.com/documentation/api-credits), [Exa pricing](https://exa.ai/pricing), or [Parallel pricing](https://docs.parallel.ai/getting-started/pricing). Saved-page reading and searching do not incur another service request. Pages requiring authentication or interactive challenges can still fail; the Tool reports missing content instead of claiming a complete extraction.

## Channels

Telegram, Discord, Slack, Mattermost and WhatsApp Channels route inbound messages to one Identity Agent. Project Agents cannot own a Channel. Add bot credentials to the process environment or `<data-dir>/.env`, then configure the token variable name rather than the token itself:

```dotenv
TELEGRAM_BOT_TOKEN_MAIN=...
DISCORD_BOT_TOKEN_MAIN=...
```

Create and manage a Channel in Settings or with positional Channel ids in the CLI:

```bash
vbot channel add tg-main --platform telegram --agent assistant --token-env TELEGRAM_BOT_TOKEN_MAIN
vbot channel list
vbot channel status tg-main
vbot channel update tg-main --allow 123456789
vbot channel identity tg-main
vbot channel identity tg-main --user 123456789
vbot channel access tg-main --group -100123456789
vbot channel admin grant tg-main --group -100123456789 --user 987654321
vbot channel admin revoke tg-main --group -100123456789 --user 987654321
vbot channel enable tg-main
vbot channel disable tg-main
vbot channel remove tg-main
```

An empty allowlist means deny all inbound chats, not allow everyone. To discover an id safely, message the bot once and inspect `vbot channel status <channel-id>` or Settings; each active adapter keeps the 20 most recent denied chats in memory. Allowing a chat restarts the adapter and clears that observation list. The allowlist gates inbound traffic only; an Agent using `channel_send` with an explicit platform target can send to any supported chat the bot account can reach. WhatsApp is limited to your self chat in both directions.

Group behavior is configurable from the CLI with `--response-mode mention|all`, list-replacing `--mention-pattern`, and `--observe-unaddressed true|false`. `channel identity` shows or sets the Channel account's own identity from previously seen participants; that identity is an admin in every group and cannot be demoted. `channel access` lists durable participants and roles for one group. `admin grant` and `admin revoke` are additive, idempotent one-user actions scoped to that group. Channel create/update/enable/disable output returns the saved config; `channel status` separately reports listener health and denied chats.

Direct-message Session routing is controlled by `dm_scope`: `per_conversation` is the default, while `main`, `per_peer`, and `per_account_channel_peer` provide broader or narrower sharing. Group chats always use a shared conversation anchor. `/new` advances that anchor to a new active Session without changing the Agent-wide current Session used by WebUI and Desktop.

Groups respond only when addressed by default: a platform mention, a reply to the bot, or a configured case-insensitive mention regex. Telegram also derives an exact case-insensitive wake name from the bot's current visible Telegram name at Channel start; BotFather privacy mode must be disabled for Telegram to deliver these plain name-addressed group messages. `response_mode: all` responds to every allowed group message. With `observe_unaddressed` enabled, otherwise-unaddressed group messages become untrusted background notes without starting a Run. Every seen group participant is assigned `admin` or `member` by stable platform user id: admins retain the Agent's full Tool access, while members may authorize only `web_search` and `web_fetch`. Group Built-in Commands and reserved `run:` button taps require `admin`. Role enforcement is dispatch-only, so grants/revokes do not alter the System Prompt or provider Tool definitions; a grant affects new ingress only, while a revoke applies before the next Tool call of an active admin Run. DMs remain governed by the chat allowlist.

All adapters ingest supported media and files and preserve Channel Session context. Telegram and Discord show activity and reply to the triggering group message. Telegram supports outbound inline buttons and deterministic Extension tap handlers; the other adapters reject button payloads. A reserved `run:<payload>` Telegram button wakes the Agent with the complete keyboard state instead of invoking an Extension handler.

Channels serialize work per conversation and share bounded Queue capacity. Only the final Assistant text from a completed Run is relayed; reasoning, Tool events, and intermediate output remain available in the vBot Session and server event streams.


### WhatsApp: use your existing account

This Channel links vBot as a device to your existing WhatsApp account using [Baileys](https://github.com/WhiskeySockets/Baileys). You keep your number and phone app; no second SIM is required. This is an unofficial connection, not a WhatsApp Business bot. Account restrictions and upstream protocol changes are possible.

1. Install Node.js **22 or newer** with npm on the machine running the vBot server. A packaged vBot installation also needs this optional prerequisite.
2. In **Settings > Channels**, create a WhatsApp Channel for your Agent. Keep the allowed chat ID `self`. It starts disabled until setup/pairing.
3. Select **Install WhatsApp support**. This downloads pinned dependencies into the Channel's data directory; wait until installation finishes.
4. Select **Connect WhatsApp**, then scan the QR from your phone's **Settings > Linked devices > Link a device**.
5. Open your WhatsApp self chat (message yourself) and send a new message. Only messages you write there can start Runs; other conversations and vBot's own replies are ignored.

The same server operations are available through the CLI. QR codes remain in Settings, never terminal output:

```bash
vbot channel add wa-main --platform whatsapp --agent assistant --allow self
vbot channel whatsapp setup wa-main
vbot channel whatsapp status wa-main
vbot channel whatsapp pair wa-main
```

Setup is asynchronous: inspect status until `installed: True`, then pair. No token flag is accepted. `self` is also the only supported `channel_send` target. An empty allowlist disables inbound messages. The connection restores saved pairing after server restarts. After logging out the linked device, use **Link again with a new QR code** or `vbot channel whatsapp pair wa-main --reset`. Re-pairing keeps the old auth files in a `revoked_*` directory; remove old linked devices in WhatsApp itself. Protect `<data-dir>/channels/<id>/whatsapp/` like account credentials. If a vBot update requires newer bridge files, disable the Channel and run setup again.

### Slack

Slack uses its official [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/) connection, so the vBot server needs no public webhook URL. Create a Slack app, enable Socket Mode and create an **app-level token** (`xapp-…`) with `connections:write`. Separately install the app to your workspace to obtain its **bot token** (`xoxb-…`). These are different credentials and require different variable names.

Configure these bot scopes before installation (reinstall the app after changing scopes):

- `chat:write`, `files:read`, `files:write`
- `channels:read`, `channels:history`, `groups:read`, `groups:history`
- `im:read`, `im:history`, `mpim:read`, `mpim:history`

Under Event Subscriptions, subscribe to bot events `message.channels`, `message.groups`, `message.im` and `message.mpim`. Enable the App Home Messages tab and allow users to send messages. Invite the bot to each channel it should use, including private channels. An `app_mention` subscription alone does not supply the message events this adapter consumes.

In vBot Settings choose Slack and enter the bot-token and app-token variable names. Both must already resolve in the server's environment or data-dir `.env`. Alternatively, save the Channel disabled, then supply each secret separately through UTF-8 stdin:

```bash
vbot channel add slack-main --platform slack --agent assistant --token-env SLACK_BOT_TOKEN --app-token-env SLACK_APP_TOKEN --disabled
vbot channel token set slack-main --stdin
vbot channel token set slack-main --slot app --stdin
vbot channel enable slack-main
vbot channel status slack-main
```

The `token set` commands read their respective secret from stdin; never put a token in an argument. Send the bot a DM or a channel message, inspect denied chats in Settings/status, then allow the exact conversation ID (`D…`, `C…` or `G…`). IDs are strings, not numbers. In mention-only groups, mention the bot or use a configured wake regex, including inside threads. Messages in threads share the parent conversation's Session; responses return to the thread. File upload uses Slack's current external-upload flow.

### Mattermost

Use an existing Mattermost server and create a [bot account](https://developers.mattermost.com/integrate/reference/bot-accounts/) with a token. The server administrator may need to enable bot accounts. Add the bot to the relevant team/channels and grant the permissions needed to read messages, post and share files. vBot connects to the server's REST API and WebSocket; no public vBot webhook is required.

In Settings choose Mattermost, enter the server URL (for example `https://chat.example.org`, including a deployment subpath if present) and the bot-token variable name. Do not include `/api/v4` or credentials in the URL. Managed-token setup is also available:

```bash
vbot channel add mm-main --platform mattermost --agent assistant --server-url https://chat.example.org --token-stdin
vbot channel status mm-main
vbot channel update mm-main --allow <conversation-id>
```

Send a DM or channel message to discover its ID through denied chats. Allowlist entries are Mattermost conversation IDs, not team IDs or usernames. Group mention policy and thread behavior match the Slack behavior described above.

### Initial integration coverage

WhatsApp, Slack and Mattermost support incoming text/media, outgoing text/files, shared Commands, Session routing and Queue/access enforcement. Slack and Mattermost currently have no typing indicator, inline buttons, fetched quoted-message content, or offline history backfill. Mention-only group threads still need an explicit mention or wake regex. Recent received IDs are retained across restarts to suppress replays; this does not guarantee exactly-once processing after a crash.

Connection handshakes, routing, file APIs, access rejection, WhatsApp self-chat/echo filtering, credential handling and QR setup are covered by local automated tests. Pairing and end-to-end delivery with real accounts still need a live smoke test.

## Cron

Cron schedules one-time or recurring Agent Runs. Names default from the prompt when omitted and need not be unique; the generated job id is the identity. A job may target an Identity Agent or `agent@project`, use an existing Session, or create a fresh Session each time it fires.

```bash
vbot cron create assistant --name "Morning priorities" --prompt "Summarize today's priorities" --cron "0 9 * * *"
vbot cron create reviewer@my-project --name "Repository review" --prompt "Review the repository status" --every 60 --repeat 3
vbot cron list
vbot cron show JOB_ID
vbot cron update JOB_ID --status paused
vbot cron enable JOB_ID
vbot cron disable JOB_ID
vbot cron delete JOB_ID
```

Recurring expressions contain exactly five fields and have a minimum cadence of one minute. On create, omitting `--session` gives each fire a fresh Session. On update, omission preserves the target; `--clear-session` restores fresh Sessions. Use `show` to read the full prompt before replacing it. `--every` takes whole minutes; `--repeat` limits future fires. Invalid individual job records are skipped and preserved for repair; a malformed Cron store disables scheduling and blocks mutations rather than overwriting the source.

## Bootstrap

Bootstrap schedules an Agent Run after the server has fully reached its ready point. It is CLI-only and supports a one-shot next-startup check or one Run after every startup. Multiple jobs may run in the same startup; jobs targeting the same Session stay ordered while independent Sessions can run concurrently.

```bash
vbot bootstrap create assistant --name "Startup health" --prompt "Check server status and logs" --mode always
vbot bootstrap create --current-session --name "Verify vBot update" --prompt "Check server status and the latest logs, then report whether the update succeeded. Do not repeat the update." --mode once
vbot bootstrap list
vbot bootstrap show JOB_ID
vbot bootstrap update JOB_ID --prompt "Check status, logs, and Provider health"
vbot bootstrap disable JOB_ID
vbot bootstrap enable JOB_ID
vbot bootstrap delete JOB_ID
```

Creation requires `--mode once|always`. A job created, updated, or enabled is armed for a future startup and cannot fire immediately in the current process. `--current-session` is available only from Bash inside a vBot Run and uses that Run's exact Agent/Project/Session context; otherwise pass an Agent address and optional `--session` explicitly. A completed one-shot is immutable history. Failed one-shots may be rearmed with `enable`.

Packaged Windows updates called from a vBot Run automatically arrange their own once-Bootstrap in the same Session; do not add another. Human and tray updates do not create a Bootstrap or Agent. For a source-checkout update from a Run, create a one-shot Bootstrap with `--current-session` before a restart, verify its full prompt and target with `vbot bootstrap show JOB_ID`, and give it a prompt that runs `vbot server status`, `vbot log list`, and `vbot log read` on the latest log before reporting the result. `vbot update --no-restart` does not need a Bootstrap unless a later startup check is wanted.

## Extensions and Home Assistant

Extensions are trusted Python code loaded into the Runtime process. They may register Tools, hooks, Recall backends, System Prompt blocks, settings fields, Channel interaction handlers, and Skills. Because they run with the same OS permissions as vBot, install only code you trust.

vBot scans direct children of `<data-dir>/extensions/` plus configured Extension roots. Supported entry points are a `.py` file, a package `__init__.py`, or a package `extension.py`. Code changes can be reloaded live:

```bash
vbot extensions list
vbot extensions reload
vbot extensions enable homeassistant
vbot extensions show homeassistant
```

Inspect an Extension with `extensions show <name>` and change a field with `extensions set <name> <field> <value>`. Use `--stdin` for secrets so they do not enter shell history:

```bash
vbot extensions set homeassistant url http://homeassistant.local:8123
Get-Content .\hass-token.txt | vbot extensions set homeassistant token --stdin
```

For the Extension API, hook contracts, capabilities, and examples, use the [vbot-cli Skill](resources/skills/vbot-cli/SKILL.md), its [Extension authoring guide](resources/skills/vbot-cli/references/extensions.md), and its [runnable templates](resources/skills/vbot-cli/assets/extensions). Bundled Swarm, MCP, and Computer Use operation is covered in the Skill's [Extension usage reference](resources/skills/vbot-cli/references/extension-usage.md).

### Home Assistant

Home Assistant ships as a bundled Extension. In Settings → Extensions → Home Assistant, enter the server URL and a Long-Lived Access Token created from the Home Assistant profile Security page. The secret is stored under `HASS_TOKEN` in the data-directory `.env` and is never returned by the read API. Changes take effect without a restart; until a token exists, the Tools remain hidden and the Extension reports that it is waiting for configuration.

| Tool | Behavior |
|---|---|
| `ha_list_entities` | List entities, optionally filtered by domain or area |
| `ha_get_state` | Read the full state of one entity |
| `ha_list_services` | Discover services and parameters |
| `ha_call_service` | Invoke a service such as turning on a light or setting a thermostat |

`ha_call_service` blocks `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, and `rest_command` because those domains can execute arbitrary code or make outbound requests. Entity, domain, and service identifiers are validated before a request is sent.

## CLI reference

Commands read as `vbot <area> <command> [target] [options]`, with deeper subcommands where needed. Start with `vbot --help`, then `vbot <area> --help`. Collection names accept singular and plural forms, such as `vbot provider list` and `vbot providers list`.

Use `vbot help`, `vbot project`, or `vbot project override` to discover the next action without executing anything. Related actions have readable paths such as `project override set`, `session policy set`, `channel token set`, `prompt layout reset`, `task-model option set`, and `skill file write`. Existing compound spellings remain compatible. Extensions use `extensions show <name>`, `extensions set <name> <field> <value>`, and `extensions run <name> <operation>`.

Management commands share an explicit completion marker: `OK` for a completed command, `WARN` for pending work or a reported limitation, and `ERROR` for failure. This does not turn saved configuration into proof of runtime readiness. Notices go to stderr; stdout retains complete data and content, including JSON. Long commands report elapsed time and available operation phases before completion. Terminals get color and status symbols where supported, with long record rows arranged as separate fields. `NO_COLOR=1` disables color. `--output plain` preserves the stable data layout and suppresses extra progress/completion notices; `--output human` requests readable record layout in a capture. Update, server lifecycle, and Doctor retain their dedicated reports.


```bash
vbot server restart
vbot providers list
vbot provider connect openai
```

The last command starts OpenAI Subscription sign-in. OAuth commands select the only OAuth Connection automatically; when there are multiple candidates, specify `--connection` using an id from the displayed list. `vbot provider list --details` preserves all Connection and Account fields; `provider status <provider-id>` narrows those details to one Provider. The Provider overview distinguishes configured, disabled, missing-credential and local reachability states; configured does not establish live upstream access.

Installed commands use `vbot`. From a source checkout, `python cli/main.py` and `python -m cli.main` expose the same parser. Most management commands call the running server through RPC and accept `--host`, `--port`, and `--data-dir` on the leaf command. Server lifecycle, home, desktop, update, uninstall, autostart, doctor, and `data-store` offline maintenance include local work and do not merely proxy management RPC.

| Area | Commands |
|---|---|
| Server | `server start`, `server stop`, `server restart`, `server status` |
| Paths | `home [--data-dir ...]` |
| Desktop | `desktop [--host ... --port ...]` |
| Installation lifecycle | `update`, `uninstall`, `autostart enable`, `autostart disable`, `autostart status` |
| Agents | `agent list`, `agent show`, `agent create`, `agent update`, `agent rename`, `agent reorder`, `agent delete` |
| Projects | `project add`, `project list`, `project show`, `project set`, `project override set`, `project override clear`, `project detect`, `project remove` |
| Sessions | `session list`, `session create`, `session fork`, `session rename`, `session policy set`, `session delete`, `session channel link` |
| Data store | `data-store status`, `data-store snapshot list|create|verify|restore`, `data-store incident acknowledge`, `data-store unregister` |
| Channels | `channel add`, `channel list`, `channel update`, `channel token set`, `channel enable`, `channel disable`, `channel status`, `channel identity`, `channel access`, `channel admin grant`, `channel admin revoke`, `channel whatsapp setup/status/pair`, `channel remove` |
| Tools and Skills | `tool list`, `skill list`, `skill inventory`, `skill inspect`, `skill install`, `skill read`, `skill enable`, `skill disable`, `skill share`, `skill unshare`, `skill create`, `skill update`, `skill delete`, `skill file write`, `skill file remove` |
| Memory | `memory list`, `memory add`, `memory replace`, `memory remove` |
| System Prompt | `prompt list`, `prompt show`, `prompt update`, `prompt reset`, `prompt create`, `prompt remove`, `prompt layout set`, `prompt layout reset`, `prompt preview` |
| Providers | `provider list`, `provider status`, `provider usage`, `provider history list`, `provider history clear`, `provider custom list`, `provider custom save`, `provider custom delete`, `provider key set`, `provider key unset`, `provider enable`, `provider disable`, `provider connect`, `provider disconnect`, `provider connection status` |
| Models | `model list`, `model show`, `model refresh`, `task-model list`, `task-model target list`, `task-model option list`, `task-model set`, `task-model option set`, `task-model option unset`, `task-model clear` |
| Extensions | `extensions list`, `extensions reload`, `extensions enable`, `extensions disable`, `extensions show <name>`, `extensions set <name>`, `extensions operations <name>`, `extensions run <name> <operation>` |
| Cron | `cron list`, `cron show`, `cron create`, `cron update`, `cron delete`, `cron enable`, `cron disable` |
| Bootstrap | `bootstrap list`, `bootstrap show`, `bootstrap create`, `bootstrap update`, `bootstrap delete`, `bootstrap enable`, `bootstrap disable` |
| Statistics | `statistics overview`, `statistics usage`, `statistics runs`, `statistics compactions`, `statistics errors`, `statistics tools`, `statistics skills` |
| Configuration | `config list`, `config describe`, `config effective`, `config raw`, `config get`, `config set`, `config unset`, `config patch`, `doctor settings`, `doctor config` |
| Diagnostics | `log list`, `log read`, `debug status`, `debug traces`, `debug trace`, `debug clear`, `debug probe`, `performance status`, `performance record start`, `performance record stop`, `performance recordings` (alias `perf`) |

`config set <path> --stdin` reads an exact JSON value without shell-quoting loss. `log read` prints the latest 100 entries by default; `--limit 0` prints all, and `--level` filters first. Provider credential writes and Extension activation can save successfully while a later refresh/load check fails; the nonzero exit and output preserve both outcomes.

Representative syntax:

```bash
vbot provider status openai --connection openai:subscription
vbot provider usage --connection openai:subscription
vbot model refresh openai
vbot statistics usage --since 2026-07-01
vbot log read 2026-07-18.log --level error --limit 100
vbot debug probe openrouter --connection openrouter:api-key
vbot performance record start --label slow-chat --max-seconds 120
vbot performance record stop
```

`performance status` shows the slowest operations since server start, key process and Event Loop gauges, and recent Event Loop stalls with their top stack frames. A Recording captures a timeline of RPC calls, Runs, Tool calls, database transactions and worker pools until `record stop` or its `--max-seconds` limit (default 300, at most 3600); stop prints the trace file path to open at https://ui.perfetto.dev. Only one Recording runs at a time. Traces hold timings, ids and code locations, never message content.

Run `vbot <area> --help` and `vbot <area> <command> --help` for every flag and positional argument. Primary Agent, Project, Session, Channel, Provider, task type, Cron job, and Extension identifiers shown in the examples are positional unless the leaf help explicitly names an option.

## Server API

The server exposes one JSON RPC endpoint, per-Run SSE, app-wide WebSocket events, Log streaming, attachments, speech, images, and health. The server has no built-in authentication; treat access as host-level code-execution authority.

Data-store operations exposed through RPC are `data_store.status`, `data_store.snapshot_create`, `data_store.incident_acknowledge`, and `data_store.unregister`. They return safe health and recovery metadata, publish `resource_changed` with kind `data_store` after successful snapshot, acknowledgement, or unregister mutations, and never include Session content. Offline snapshot listing, verification, and restore stay in the CLI because they must inspect and control the exact local target.

### RPC envelope

Send `{"method":"...","params":{...}}` to `POST /api/rpc`. Responses use an `ok` result or a structured error. Product paths create Sessions explicitly before Chat input.

The PowerShell example below creates a Session for an Identity Agent. Use `agent@project` as `agent_id` for a Project Agent:

```powershell
$base = "http://127.0.0.1:8420"

$createBody = @{
  method = "session.create"
  params = @{
    agent_id = "coder"
  }
} | ConvertTo-Json -Depth 5

$sessionResponse = Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $createBody
$sessionId = $sessionResponse.result.session_id
```

### Send and stream Chat

`chat.send` normally waits for the admitted Run and returns its complete result. If the Session is already busy, it can return a queued descriptor instead. A recognized Built-in Command can return a command outcome without starting an ordinary Run.

```powershell
$sendBody = @{
  method = "chat.send"
  params = @{
    agent_id = "coder"
    session_id = $sessionId
    content = "Say hello in one short sentence."
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $sendBody
```

`chat.stream` returns a `run_id` and `sse_url` for an admitted Run, or a queued descriptor when the Session is busy:

```powershell
$streamBody = @{
  method = "chat.stream"
  params = @{
    agent_id = "coder"
    session_id = $sessionId
    content = "Explain vBot in two sentences."
  }
} | ConvertTo-Json -Depth 5

$streamResponse = Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $streamBody
$runId = $streamResponse.result.run_id
$sseUrl = $streamResponse.result.sse_url
curl.exe -N "$base$sseUrl"
```

The complete per-Run timeline can contain `run_started`, `user_message_persisted`, reasoning and output deltas, Tool start/delta/stdout/stderr/result events, `assistant_output`, Model fallback and Usage events, Compaction, and terminal `run_completed`, `run_failed`, or `run_cancelled` events.

Cancel a Run by id:

```powershell
$cancelBody = @{
  method = "chat.cancel"
  params = @{
    run_id = $runId
    reason = "user"
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $cancelBody
```

### HTTP and WebSocket endpoints

- `GET /health` — exact vBot health probe
- `POST /api/rpc` — JSON RPC
- `GET /api/runs/{run_id}/events` — per-Run SSE timeline
- `WS /ws` — app-wide lifecycle, reconnect, presence, and resource-change WebSocket events
- `WS /ws/logs` — selected Log WebSocket stream
- `POST /api/upload` — upload an attachment
- `GET /api/attachments/{attachment_id}` — download an attachment
- `POST /api/speech/transcribe` — transcribe audio
- `POST /api/speech/synthesize` — synthesize speech
- `GET /api/speech/artifacts/{artifact_id}` — retrieve a generated speech artifact

## Development and verification

### Local features in a packaged Windows application

Prepare a separate development checkout of the exact installed revision:

```bash
vbot customize prepare
vbot customize status
```

Edit only the returned source directory. Git and Node.js/npm must already be
available for this explicit development workflow. Add tests for the intended
behavior, then check and try the candidate:

```bash
vbot customize check --intent "Describe the fix or local feature"
vbot customize test
vbot customize activate
```

Checking runs the repository quality gates and WebUI build, records the source
revision and contents, and prepares a candidate with its dependencies. Changes
after checking require another check. The foreground test uses fresh data and a
separate port, with Extensions, Channels, Cron, Calendar and Bootstrap disabled;
it does not copy production credentials or Sessions. Activation uses the same
durable operation as an update.

Official updates carry local commits forward in a separate checkout. If changes
conflict, the running version and conflict evidence remain intact. Resolve the
files in the reported checkout, run `vbot customize rebase --intent "..."`, then
activate the checked candidate. Never resolve this by editing a released version
directory or discarding the user's local feature without their instruction.

Extension source remains in `<data-dir>/extensions/` or its configured roots and
keeps its normal reload behavior. Extra Python dependencies use a complete
managed recipe instead of changing the base runtime:

```bash
vbot application dependencies install --requirements requirements.txt
vbot application dependencies status
vbot server restart
```

Each install command replaces the complete recipe. Requirements must remain
compatible with the base runtime's packages. Updates prepare a compatible
dependency generation before stopping the server; previous generations are
retained for recovery.

### Source checkout development

Install the development dependencies and WebUI packages:

```bash
pip install -e ".[dev]"
cd webui
npm ci
cd ..
```

Run the Python server and Vite development server separately:

```bash
python server/main.py
```

```bash
cd webui
npm run dev
```

Build or preview the production frontend:

```bash
cd webui
npm run build
npm run preview
```

Repository quality gates are:

```bash
python scripts/quality.py
python scripts/quality-frontend.py
```

The Playwright E2E suite under `tests/e2e/` is separate from the local quality scripts because it controls a real server and browser environment. Local runs remain explicit opt-in and follow the repository workflow instructions; Release CI calls the same reusable Chromium job as a required pre-publish gate.

Windows release builders and maintainers should also read the
[native packaging guide](scripts/windows/README.md). Building artifacts does not
publish them or migrate an existing source installation.

## Operational notes

- vBot is alpha software. Back up the data directory before upgrades or manual config surgery.
- Agents and trusted Extensions run with the OS permissions of the account that starts vBot. Keep the server on localhost unless a deliberately secured remote topology is required.
- A remote Desktop Client should reach the server only through a trusted LAN or VPN plus restrictive firewalling, or through an authenticated TLS reverse proxy. Never expose the unauthenticated vBot port to the public internet.
- Bind conflicts normally mean another process already owns the selected port. Use `vbot server status`, choose another `--port`, or stop the conflicting process.
- Process environment credentials override values in `<data-dir>/.env`; removing a data-directory key may therefore leave a Connection configured through the process environment.
- Attachment, speech, and image artifacts are durable. Attachments currently have no garbage collector or reference counting, including attachments promoted from images read from disk.
- Complete Bash process output under `temp/bash/` is retained for 72 hours after completion; Sub-Agent activity files under `temp/subagents/` are retained for 24 hours. These temporary files supplement canonical Session history.
- Recall indexes are derived and disposable; deleting `<data-dir>/recall/` does not delete canonical Sessions.
- A database recovery incident stays visible until explicit acknowledgement. Preserve its quarantine bundle and verified snapshots; never delete evidence as part of acknowledgement.
- Update protection creates a verified data snapshot of every canonical database and the JSON configuration documents before replacing current-format code.
- Desktop echo cancellation covers audio played through the Windows default output device. Without it (turned off, another output device, or on Linux), loudspeaker output can trigger a sensitive wakeword Model; choose device placement and sensitivity accordingly.
