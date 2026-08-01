# Usage

This guide covers installing, configuring, operating, and integrating vBot. For the short product overview and quickest installation path, start with [README.md](README.md).

## Contents

- [Installation](#installation)
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
- [Extensions and Home Assistant](#extensions-and-home-assistant)
- [CLI reference](#cli-reference)
- [Server API](#server-api)
- [Development and verification](#development-and-verification)
- [Operational notes](#operational-notes)

## Requirements

- Python 3.11 or newer
- Git for installs and updates
- Node.js and npm only for a development install or a current-checkout WebUI build; release installs download a prebuilt WebUI
- At least one usable Provider Connection and Model before an Agent can run

## Installation

### Public Installer contract

The complete public installers are `scripts/install.sh` for Linux and `scripts/install.ps1` for Windows. A default install selects the latest release, clones it into `~/vbot`, creates `~/vbot/.venv`, downloads the matching WebUI, installs vBot into that isolated environment, exposes the `vbot` command, enables autostart, and starts the server. Runtime state remains separate under `~/.vbot`.

When the same Installer is executed from a vBot checkout without an explicit installation directory or version, it installs that checkout into `<checkout>/.venv` and builds the WebUI locally. It never requires or modifies the system Python environment. An internal `scripts/setup.*` helper performs checkout-local package configuration after the public Installer has established the checkout and environment; it is not an end-user installation entrypoint.

### Fresh Debian-like Linux install

The Linux Installer automates prerequisite installation through `apt` on Debian-like systems, including Raspberry Pi OS:

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash
```

Installer options are:

| Option | Meaning |
|---|---|
| `--dir <path>` | Installation directory; default `~/vbot` or `$VBOT_DIR`, or the current checkout when invoked there |
| `--version <tag>` | Install a specific release, for example `v0.1.11`; cannot be combined with `--dev` |
| `--dev` | Fresh install: track `main`; current checkout: add development dependencies; either path builds the WebUI locally and requires Node.js |
| `--data-dir <path>` | Runtime data directory; default `~/.vbot` |
| `--host <host>` | Server bind host; default `127.0.0.1` |
| `--port <port>` | Server port; default `8420`, or the existing Settings value when not explicitly overridden |
| `--desktop` | Add the Desktop accessor to a server install |
| `--desktop-client` | Install only CLI and Desktop for a remote-server client machine |
| `--no-autostart` | Do not create or start the systemd user unit |
| `--skip-webui-build` | Require and reuse an existing `webui/dist`; release installs do this automatically after downloading the asset |
| `--service-name <name>` | Custom systemd user unit name without `.service`; default `vbot` |
| `-h`, `--help` | Show Installer help |

Pass every option directly after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --version v0.1.11
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --dev
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --dir ~/apps/vbot
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --no-autostart --port 9000
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --desktop
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --desktop-client
```

### Fresh Windows install

Run the default install in a normal, non-elevated PowerShell. The installer refuses an elevated shell so the checkout, virtual environment, runtime data, and server process stay owned by the user; Windows Autostart is registered as a low-privilege per-user Task Scheduler entry and needs no UAC prompt:

```powershell
irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1 | iex
```

Use a ScriptBlock when passing options:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Version v0.1.11
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Dev
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -InstallDir D:\Apps\vbot
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -NoAutostart -Port 9000
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Desktop
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -DesktopClient
```

Windows accepts `-InstallDir`, `-Version`, `-Dev`, `-DataDir`, `-HostName`, `-Port`, `-Desktop`, `-DesktopClient`, `-NoAutostart`, `-SkipWebuiBuild`, and `-TaskName` directly. `-AllowElevatedInstall` is an explicit escape hatch for disposable automation only; never use it for a persistent installation. The immediate background server, per-user Task Scheduler action, and installed `vBot Desktop` Start-menu shortcut all use windowless launch paths, so installation, sign-in, and normal app launch leave no Python console open; the normal `vbot` command remains a console application. The final summary verifies Autostart and the actual server health independently. If per-user Task Scheduler registration fails, the package install and immediate server start still complete, while the summary reports `complete with problems` and prints the exact normal-user recovery command; a live `Server URL` is shown only when the server is running.

As with any `curl | bash` or `irm | iex` command, download and inspect the script first if you do not want to execute network content directly.

### Install the current checkout

Run the same public Installer from the repository root. It detects its checkout, creates or reuses `<checkout>/.venv`, builds the WebUI unless a matching `webui/dist` is explicitly reused, records `.vbot-install.json`, and installs the selected server or Desktop shape. This path is safe on PEP 668 systems because it never installs into the system interpreter.

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

### Install shapes

| Shape | Installed groups | Local server/WebUI | Desktop | Autostart |
|---|---|---|---|---|
| Default server | `server`, `cli` | Yes | No | Yes unless disabled |
| Server + Desktop | `server`, `cli`, `desktop` | Yes | Yes | Yes unless disabled |
| Desktop Client | `cli`, `desktop` | No | Yes | Never |
| Development | `dev`, optionally `desktop` | Yes | Optional | Yes unless disabled |

`--desktop`/`-Desktop` adds Desktop to a full local server install. `--desktop-client`/`-DesktopClient` is a server-less accessor install intended to connect to a remote vBot server; it is mutually exclusive with Desktop server mode and development mode.

## Updating and uninstalling

### Updating

```bash
vbot update
```

The updater reads `.vbot-install.json` from the checkout and preserves the recorded install shape, Python executable, dependency groups, source track, server target, and WebUI revision policy. Release installs move to the newest release with a matching WebUI asset; development installs update `main` and rebuild when needed. The Windows public Installer's `vbot` command invokes the recorded Python module instead of pip's replaceable `vbot.exe`, so the running command does not lock its own launcher during dependency updates. An older Windows shim that still starts `vbot.exe` is refused before checkout mutation with a one-time source-based resume command; completing that recovery migrates the shim for future updates. On Windows, close every vBot Desktop window before updating: the updater refuses to change the checkout while this installation's exact `vbot-desktop.exe` is running and checks again immediately before pip changes the environment. A Desktop install also refreshes its installer-owned Start-menu shortcut to the current windowless GUI launcher. If pip still fails after a checkout advance, the failure output includes the same source-based resume command through the recorded Python executable, which remains usable even if pip removed the normal `vbot` launcher. Runtime data is not modified.

Local changes to tracked checkout files require an explicit policy:

```bash
vbot update --stash
vbot update --discard
vbot update --no-restart
```

`--stash` reapplies the changes after updating. `--discard` permanently discards tracked local changes. `--no-restart` leaves the updated server stopped or running as-is.

### Uninstalling

```bash
vbot uninstall
```

The interactive command reports the Installer-recorded target and offers:

- application only — removes Autostart, launchers, and the managed application while preserving the data directory
- data only — permanently deletes the exact data directory while preserving the application
- application and data — removes both

Data-removing scopes display the resolved data path and require typing `DELETE`. A data-only reset stops the target server before deletion. If it was running, it restarts with fresh data afterward; if it was stopped, it remains stopped. A systemd-owned server is stopped and started through its existing unit so Autostart ownership is preserved.

Non-interactive callers must supply one scope and `--yes`:

```bash
vbot uninstall --app-only --yes
vbot uninstall --data-only --yes
vbot uninstall --all --yes
```

For a Desktop Client, `--app-only` and targetless `--all` remove only the local CLI/Desktop installation; they never resolve, probe, stop, or delete a default local server. A targetless `--data-only` is rejected because this install shape owns no server data. To deliberately reset a server data directory from a Desktop Client, provide `--host`, `--port`, and `--data-dir` together.

Use `--host`, `--port`, and `--data-dir` to override the recorded target. Custom Autostart names can be supplied with `--task-name` on Windows or `--service-name` on Linux. The command refuses protected roots, the home directory, any data target containing the application installation, a data target containing the caller's current directory, and application removal while the caller is inside the installation directory.

Application-removing scopes for server-owning installations first stop the exact selected server; if that stop fails, removal aborts and reports the preserved application directory. They then delegate to the bundled platform Uninstaller, which verifies the stop again before removing anything. A manifest-backed Desktop Client skips both server-stop stages unless explicit data removal selected a complete target. A fresh managed install is removed wholesale; an install performed in an existing checkout removes the installer-owned `.venv` and launcher but preserves the checkout. Desktop Start-menu/application-menu entries are removed only when the recorded install shape owns the Desktop accessor; uninstalling a server-only shape preserves any Desktop entry owned by another installation. Windows requests elevation and launches a helper that waits for the calling `vbot.exe` to exit before deleting its environment. The caller reports the absolute application directory and says explicitly that helper launch is not completed removal; the elevated window reports final completion or failure. Cancelling UAC leaves the installation and selected data in place, although a server stopped during preflight remains stopped. The underlying `scripts/uninstall.ps1` and `scripts/uninstall.sh` retain the same mandatory-stop contract for server-owning recovery and direct-setup entrypoints.

## First-run setup

Open `http://127.0.0.1:8420/`. Runtime creates an initial Identity Agent and its first Session automatically. A fresh server installation seeds the global Agent defaults with temperature `0.1` and thinking effort `high`; the initial Agent and later Agents inherit them unless configured more specifically. Existing `settings.json` files are preserved during installation and updates, so this fresh-install policy does not change an existing instance. The setup guide connects a Provider or OAuth subscription and selects a Model; creating another Agent is optional.

The WebUI is the easiest place to manage Connections and Accounts. Equivalent CLI examples are:

```bash
vbot provider set-key openrouter YOUR_KEY --refresh-models
vbot provider connect openai --connection openai:subscription
vbot provider usage --connection openai:subscription
vbot provider enable ollama
vbot model refresh openrouter
```

A key passed to `provider set-key` may be retained by shell history. Prefer the WebUI, a protected environment variable, or a shell-specific history-safe workflow when entering a real secret.

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

Use `"auth": "api_key"` for standard `Authorization: Bearer` authentication and enter the key through Settings or `provider custom-save --api-key`. Omit or clear `models_endpoint` to use manual Models only. Discovery and manual Models can coexist: a manual record overrides discovered facts for the same wire id, while other discovered Models remain available. Saving through the WebUI/RPC reloads Providers and Models immediately; after direct file editing, validate with `vbot doctor settings` and restart vBot.

```bash
vbot provider custom-save local-ai --name "Local AI" --base-url http://127.0.0.1:8080/v1 --auth none --models-endpoint /models --model chat-model
vbot provider custom-list
vbot model refresh local-ai
vbot provider custom-delete local-ai
```

`custom-save` replaces the complete Custom Provider record; repeated `--model` flags create conservative chat Model entries. Use the WebUI for the full manual capability editor. Deleting a Custom Provider removes its generated data-directory API keys but deliberately keeps Agent/default/task Model references, which remain visible as unavailable until reconfigured.

## Data directory and configuration

The normal runtime data directory is `~/.vbot`. Select another target with `--data-dir` on server and RPC-backed CLI commands, or set `VBOT_DATA_DIR`. Run `vbot home` to print the absolute application and currently selected data directories; pass `--data-dir` to inspect an explicit target.

Setup, Runtime, direct `core/storage/layout.py` use, and managed Worktree creation all initialize this same non-destructive structure:

```text
<data-dir>/
├── artifacts/
│   ├── attachments/
│   ├── images/
│   ├── speech/
│   ├── models/
│   ├── debug/
│   └── temp/
│       ├── atomic/
│       ├── bash/
│       └── subagents/
├── statistics/
│   └── provider-usage/
├── agents/
├── archive/
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
├── .env
└── settings.json
```

`artifacts/attachments/`, `artifacts/images/`, `artifacts/speech/`, `artifacts/models/`, and `artifacts/debug/` contain durable domain-owned artifacts. The three `artifacts/temp/` children remain separate: atomic replacement staging, 72-hour retained Bash output, and 24-hour retained Sub-Agent activity. `statistics/provider-usage/` is normalized Provider-owned upstream history; local Statistics still derive from Sessions and add no persistence. The tracked system Model DB remains `resources/models/`; only the complete instance runtime Model DB moves under `artifacts/models/`.

Independent roots keep their established ownership: `agents/` contains Identity Agent configs, default Workspaces, private Skills, and Sessions; `projects/` contains Project metadata and Project Agent Sessions; `skills/` contains global user Skills; `channels/`, `cron/`, `extensions/`, `prompts/`, `recall/`, `logs/`, `oauth/`, and `archive/` retain their existing records. Custom absolute Workspaces remain outside the data directory. `processes/` is reserved for future persistent process records and is not the Bash-output location.

The initializer copies `resources/data-dir/.env.example` only when `.env` is absent and creates an empty `settings.json` only when Settings is absent. It never rewrites either existing file; Setup separately retains ownership of fresh-install Settings defaults and explicit port updates.

### Converting an existing data directory

Current application code reads only the canonical paths. Convert every older data directory explicitly before starting the updated instance; Setup and Runtime do not migrate or dual-read old paths.

1. Stop the exact vBot instance that uses the target data directory.
2. Back up the complete data directory with your normal filesystem backup mechanism.
3. From the updated vBot checkout, run the converter without `--apply` and resolve every reported unsupported legacy `temp/` entry or destination collision:

   ```bash
   python scripts/converters/data_dir_artifacts_layout.py <data-dir>
   ```

4. Apply the same preflighted conversion while vBot remains stopped:

   ```bash
   python scripts/converters/data_dir_artifacts_layout.py <data-dir> --apply
   ```

5. If the data predates extension-bearing Attachment blobs, run the Attachment schema converter after the structural conversion:

   ```bash
   python scripts/converters/attachment_blob_extensions.py <data-dir>
   ```

6. Validate all configuration files:

   ```bash
   vbot doctor config --data-dir <data-dir>
   ```

7. Start the instance and verify its reported data root, Agents and Sessions, artifact serving, Debug status when enabled, and Provider usage history.

The structural converter defaults to a read-only preflight, rejects symlinks, special files, unknown legacy temporary categories, and every matching destination, and never overwrites data. Apply moves regular leaf files atomically within the data root and is resumable after interruption. Retired `temp/skill-drafts/` content is preserved in place but ignored by current vBot; remove it manually only after confirming it contains nothing you want to recover. Previously persisted absolute paths in old Session text are not rewritten.

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

Bare `vbot config` is equivalent to `config list`; `config get`, `set`, `unset`, and atomic multi-operation `patch` use public paths rather than internal `settings.json` keys. `config effective` shows the normalized public document, while `config raw` is diagnostic only. `doctor settings` strictly validates the target `settings.json`; `doctor config` checks all user-editable JSON files. At runtime, malformed root Settings fall back safely, invalid top-level Settings sections are omitted while valid siblings remain active, and invalid individual Agent or Project records are skipped. The source file is not silently rewritten, and mutations that could overwrite invalid source state are blocked until it is repaired.

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

The WebUI at `http://127.0.0.1:8420/` provides Chat, Agent and Project management, Cron, System Prompt editing, Settings, Logs, Statistics, and Debug views. It is an Accessor over the server-owned runtime; closing the browser does not stop a Run.

Launch the optional Desktop accessor with:

```bash
vbot desktop
vbot desktop --host 192.168.1.50 --port 8420
```

On Windows, `-Desktop` and `-DesktopClient` installations also create a `vBot Desktop` Start-menu entry backed by the windowless `vbot-desktop` GUI launcher. `vbot desktop` remains the equivalent console command for terminal use.

Without explicit host and port, Desktop opens its Connection screen and auto-connects only when a remembered last-used server exists. It does not silently assume localhost. Probe failures return to the same screen with the target prefilled, and the native Server menu can switch or reconnect at runtime.

Desktop loads the same server-served WebUI. A local folder picker would browse the client machine rather than a remote server, so Project paths remain server-side paths entered through the same WebUI field.

Desktop Voice runs wakeword detection and microphone capture locally. After the phrase matches, the recorded command is sent to the active server's speech transcription endpoint and routed to the server-specific Personal/Identity Agent configured in Settings → Voice. Exactly one built-in or imported openWakeWord ONNX Model listens at a time. Imported Models are validated and stored on the Desktop machine; training happens outside vBot. Audio before the wake phrase is not uploaded, while the captured command is sent to the configured speech backend.

## Agents, Projects, and Sessions

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

Agent create/update also expose delegation policy through `--subagent-allow <agent> ...` and Agent Policy through `--compaction-policy <json-object>` / `--clear-compaction-policy`. `--clear-model` and `--clear-fallback-model` restore global-default inheritance. Create/update output includes the saved id, Workspace, selected Project, effective Model, effective Policy, and provenance; Workspace moves also report copied and backed-up files. Creating an Agent without an Agent or global-default Model is allowed for onboarding, but the result explicitly warns that it cannot run and points to `vbot model list --task chat` followed by `agent update --model`.

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
vbot project set-override my-project orchestrator temperature 0.3
vbot session create orchestrator@my-project
```

Project registration and Team scanning never write to the repository. Normal Agent Tools may write there during a Run when the Project is the working directory.

Project `add`/`set` can replace the Tool Whitelist and bundled/global/Project Skill policy lists. `set-override`/`clear-override` manage one Project Agent's vBot-owned model, temperature, thinking-effort, or Compaction Policy tier without editing the repository. `project rm --copy-rooted-agent-files` preserves `SOUL.md`, `USER.md`, and `MEMORY.md` before rooted Identity Agents with custom Workspaces are reset to their default Workspace; removal output lists every affected Agent and file effect.

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
vbot session set-compaction-policy coder SESSION_ID --policy '{"enabled": false}'
vbot session delete coder SESSION_ID --yes
```

Deleting an Agent, Project, or Session archives its vBot-owned state rather than silently erasing it. Project source repositories are never archived or removed.

## Chat, Queue, and Built-in Commands

One Session admits one active Run. New messages sent while it is busy enter its Queue and execute in order; WebUI and Channels surface queued state. Cancellation is cooperative and stops further model or Tool progression as quickly as possible, but already-running external work may not be hard-abortable.

The composer accepts plain text, uploaded attachments, `@` file mentions, Built-in Commands, and Skill triggers. Image, audio, video, and text/file inputs are normalized into content blocks; Provider adapters receive only formats they support. Audio transcription is cached on the attachment after its first successful transcription.

Built-in Commands are owned by Chat and work in the WebUI; Telegram and Discord support the same set except `/agent`:

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

Tools are runtime capabilities exposed according to Agent, Project, Extension, and Settings policy. Inspect the public catalog and one Agent's complete System Prompt with:

```bash
vbot tool list
vbot skill list
vbot skill read --scope global
vbot skill create librarian --scope agent:coder --file SKILL.md
vbot prompt preview coder
```

The CLI Skill manager authors only global and private Identity Agent scopes; it supports `read`, `create`, `update`, `delete`, `write-file`, and `remove-file`. Project Skills stay repository-owned and bundled Skills stay read-only. The Prompt manager likewise supports default and `agent:<id>` scopes, custom user blocks, and complete layout order/enabled-state updates.

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

The result contains only Models with at least one enabled, credentialed Connection and prints the exact id accepted by `agent create --model` or `agent update --model`. It also includes the effective context window, capabilities/task types, and `reachable: no` for a local Model whose service is currently down. Additional repeatable filters are `--capability`, `--task`, `--input-modality`, and `--output-modality`; `--min-context-window` applies a token floor.

### Recall

Recall searches canonical persisted Session history; it does not replace curated `MEMORY.md`. Available first-party backends are `jsonl_scan` for direct chronological scanning, `sqlite_fts` for indexed substring and relevance search, `vector` for semantic Passage search through the configured `text_embedding` Model, and `hybrid` for fused lexical and semantic results. SQLite indexes under `<data-dir>/recall/` are derived and disposable; incompatible schemas or embedding spaces rebuild rather than migrate.

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
| `image_generation` | image generation and editing, including source-image workflows when the target supports them |

Use Settings for target-specific option forms, or inspect and bind them through the CLI:

```bash
vbot task-model list
vbot task-model targets text_embedding
vbot task-model options image_generation openai/gpt-image-1::api-key
vbot task-model set text_embedding openai/text-embedding-3-small::api-key
vbot task-model clear text_embedding
```

## Channels

Telegram and Discord Channels route inbound messages to one Identity Agent. Project Agents cannot own a Channel. Add bot credentials to the process environment or `<data-dir>/.env`, then configure the token variable name rather than the token itself:

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
vbot channel grant-admin tg-main --group -100123456789 --user 987654321
vbot channel revoke-admin tg-main --group -100123456789 --user 987654321
vbot channel enable tg-main
vbot channel disable tg-main
vbot channel remove tg-main
```

An empty allowlist means deny all inbound chats, not allow everyone. To discover an id safely, message the bot once and inspect `vbot channel status <channel-id>` or Settings; each active adapter keeps the 20 most recent denied chats in memory. Allowing a chat restarts the adapter and clears that observation list. The allowlist gates inbound traffic only; an Agent using `channel_send` with an explicit platform target can send to any chat the bot account can reach.

Group behavior is configurable from the CLI with `--response-mode mention|all`, list-replacing `--mention-pattern`, and `--observe-unaddressed true|false`. `channel identity` shows or sets the Channel account's own identity from previously seen participants; that identity is an admin in every group and cannot be demoted. `channel access` lists durable participants and roles for one group. `grant-admin` and `revoke-admin` are additive, idempotent one-user actions scoped to that group. Channel create/update/enable/disable output returns the saved config; `channel status` separately reports listener health and denied chats.

Direct-message Session routing is controlled by `dm_scope`: `per_conversation` is the default, while `main`, `per_peer`, and `per_account_channel_peer` provide broader or narrower sharing. Group chats always use a shared conversation anchor. `/new` advances that anchor to a new active Session without changing the Agent-wide current Session used by WebUI and Desktop.

Groups respond only when addressed by default: a platform mention, a reply to the bot, or a configured case-insensitive mention regex. Telegram also derives an exact case-insensitive wake name from the bot's current visible Telegram name at Channel start; BotFather privacy mode must be disabled for Telegram to deliver these plain name-addressed group messages. `response_mode: all` responds to every allowed group message. With `observe_unaddressed` enabled, otherwise-unaddressed group messages become untrusted background notes without starting a Run. Every seen group participant is assigned `admin` or `member` by stable platform user id: admins retain the Agent's full Tool access, while members may authorize only `web_search` and `web_fetch`. Group Built-in Commands and reserved `run:` button taps require `admin`. Role enforcement is dispatch-only, so grants/revokes do not alter the System Prompt or provider Tool definitions; a grant affects new ingress only, while a revoke applies before the next Tool call of an active admin Run. DMs remain governed by the chat allowlist.

Both adapters ingest supported media and files, preserve Channel Session context, show activity, and reply to the triggering group message. Telegram supports outbound inline buttons and deterministic Extension tap handlers; Discord rejects button payloads. A reserved `run:<payload>` Telegram button wakes the Agent with the complete keyboard state instead of invoking an Extension handler.

Channels serialize work per conversation and share bounded Queue capacity. Only the final Assistant text from a completed Run is relayed; reasoning, Tool events, and intermediate output remain available in the vBot Session and server event streams.

## Cron

Cron schedules one-time or recurring Agent Runs. Every job has a required human-readable name; names need not be unique because the generated job id remains its identity. A job may target an Identity Agent or `agent@project`, use an existing Session, or create a fresh Session each time it fires.

```bash
vbot cron create assistant --name "Morning priorities" --prompt "Summarize today's priorities" --cron "0 9 * * *"
vbot cron create reviewer@my-project --name "Repository review" --prompt "Review the repository status" --at "2026-07-20T10:00:00+02:00"
vbot cron list
vbot cron update JOB_ID --status paused
vbot cron enable JOB_ID
vbot cron disable JOB_ID
vbot cron delete JOB_ID
```

Recurring expressions contain exactly five fields and have a minimum cadence of one minute. A job without `--session` receives a fresh Session for every fire. Invalid individual job records are skipped and preserved for repair; a malformed Cron store disables scheduling and blocks mutations rather than overwriting the source.

## Extensions and Home Assistant

Extensions are trusted Python code loaded into the Runtime process. They may register Tools, hooks, Recall backends, System Prompt blocks, settings fields, Channel interaction handlers, and Skills. Because they run with the same OS permissions as vBot, install only code you trust.

vBot scans direct children of `<data-dir>/extensions/` plus configured Extension roots. Supported entry points are a `.py` file, a package `__init__.py`, or a package `extension.py`. Code changes can be reloaded live:

```bash
vbot extensions list
vbot extensions reload
vbot extensions enable homeassistant
vbot extensions homeassistant
```

Inspect or change an Extension field with its name-first command. Use `--stdin` for secrets so they do not enter shell history:

```bash
vbot extensions homeassistant set url http://homeassistant.local:8123
Get-Content .\hass-token.txt | vbot extensions homeassistant set token --stdin
```

For the Extension API, hook contracts, capabilities, and examples, see [docs/extensions.md](docs/extensions.md) and [examples/extensions](examples/extensions).

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

Installed commands use `vbot`. From a source checkout, `python cli/main.py` exposes the same parser. Most management commands call the running server through RPC and accept `--host`, `--port`, and `--data-dir` on the leaf command. Server lifecycle, home, desktop, update, uninstall, autostart, and doctor include local work and do not merely proxy management RPC.

| Area | Commands |
|---|---|
| Server | `server start`, `server stop`, `server restart`, `server status` |
| Paths | `home [--data-dir ...]` |
| Desktop | `desktop [--host ... --port ...]` |
| Installation lifecycle | `update`, `uninstall`, `autostart enable`, `autostart disable`, `autostart status` |
| Agents | `agent list`, `agent show`, `agent create`, `agent update`, `agent rename`, `agent delete` |
| Projects | `project add`, `project list`, `project show`, `project set`, `project set-override`, `project clear-override`, `project rm` |
| Sessions | `session list`, `session create`, `session fork`, `session rename`, `session set-compaction-policy`, `session delete`, `session link-channel` |
| Channels | `channel add`, `channel list`, `channel update`, `channel enable`, `channel disable`, `channel status`, `channel identity`, `channel access`, `channel grant-admin`, `channel revoke-admin`, `channel remove` |
| Tools and Skills | `tool list`, `skill list`, `skill read`, `skill create`, `skill update`, `skill delete`, `skill write-file`, `skill remove-file` |
| System Prompt | `prompt list`, `prompt update`, `prompt reset`, `prompt create`, `prompt remove`, `prompt set-layout`, `prompt reset-layout`, `prompt preview` |
| Providers | `provider list`, `provider status`, `provider usage`, `provider custom-list`, `provider custom-save`, `provider custom-delete`, `provider set-key`, `provider unset-key`, `provider enable`, `provider disable`, `provider connect`, `provider disconnect`, `provider connect-status` |
| Models | `model list`, `model refresh`, `task-model list`, `task-model targets`, `task-model options`, `task-model set`, `task-model clear` |
| Extensions | `extensions list`, `extensions reload`, `extensions enable`, `extensions disable`, `extensions <name>`, `extensions <name> set` |
| Cron | `cron list`, `cron create`, `cron update`, `cron delete`, `cron enable`, `cron disable` |
| Statistics | `statistics overview`, `statistics usage`, `statistics runs`, `statistics errors`, `statistics tools`, `statistics skills` |
| Configuration | `config list`, `config describe`, `config effective`, `config raw`, `config get`, `config set`, `config unset`, `config patch`, `doctor settings`, `doctor config` |
| Diagnostics | `log list`, `log read`, `debug status`, `debug traces`, `debug trace`, `debug clear`, `debug probe` |

Representative syntax:

```bash
vbot provider status openai --connection openai:subscription
vbot provider usage --connection openai:subscription
vbot model refresh openai
vbot statistics usage --since 2026-07-01
vbot log read 2026-07-18.log
vbot debug probe openrouter
```

Run `vbot <area> --help` and `vbot <area> <command> --help` for every flag and positional argument. Primary Agent, Project, Session, Channel, Provider, task type, Cron job, and Extension identifiers shown in the examples are positional unless the leaf help explicitly names an option.

## Server API

The server exposes one JSON RPC endpoint, per-Run SSE, app-wide WebSocket events, Log streaming, attachments, speech, images, and health. The server has no built-in authentication; treat access as host-level code-execution authority.

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
- `GET /api/images/artifacts/{artifact_id}` — retrieve a generated image artifact

## Development and verification

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

## Operational notes

- vBot is alpha software. Back up the data directory before upgrades or manual config surgery.
- Agents and trusted Extensions run with the OS permissions of the account that starts vBot. Keep the server on localhost unless a deliberately secured remote topology is required.
- A remote Desktop Client should reach the server only through a trusted LAN or VPN plus restrictive firewalling, or through an authenticated TLS reverse proxy. Never expose the unauthenticated vBot port to the public internet.
- Bind conflicts normally mean another process already owns the selected port. Use `vbot server status`, choose another `--port`, or stop the conflicting process.
- Process environment credentials override values in `<data-dir>/.env`; removing a data-directory key may therefore leave a Connection configured through the process environment.
- Attachment, speech, and image artifacts are durable. Attachments currently have no garbage collector or reference counting, including attachments promoted from images read from disk.
- Complete Bash process output under `temp/bash/` is retained for 72 hours after completion; Sub-Agent activity files under `temp/subagents/` are retained for 24 hours. These temporary files supplement canonical Session history.
- Recall indexes are derived and disposable; deleting `<data-dir>/recall/` does not delete canonical Sessions.
- The Desktop wakeword listener is independent of Chat text-to-speech playback, so speaker output can trigger a sensitive wakeword Model. Choose device placement and sensitivity accordingly.
