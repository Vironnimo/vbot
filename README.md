<h1 align="center">vBot</h1>

<p align="center"><i>A local-first agent harness for personal agents, project teams, tools, skills, automation, and the models you choose.</i></p>

<p align="center">
  <img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache-2.0">
  <img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status: Alpha">
</p>

vBot is a self-hosted runtime for AI agents. Use it through the WebUI, the optional Desktop app, the CLI, Telegram, Discord, or its HTTP/SSE/WebSocket interfaces. Identity Agents keep their own Workspace, Memory, Skills, and Sessions; Projects let the same runtime discover and execute an existing OpenCode or Claude Code team without copying that team out of the repository.

One async Python kernel owns all agent behavior. The FastAPI server exposes it, while the WebUI, Desktop, CLI, and Channels are Accessors rather than separate control planes.

## Features

<table>
<tr><td><b>Personal and project agents</b></td><td>Use durable Identity Agents, root one in a Project while retaining its identity, or run repository-defined Project Agents addressed as <code>agent@project</code>.</td></tr>
<tr><td><b>Many providers and models</b></td><td>Connect OpenAI, Anthropic, OpenRouter, Ollama, Mistral, GitHub Copilot, and others through API-key, OAuth, or keyless Connections, with per-Agent defaults and Run-local fallback.</td></tr>
<tr><td><b>Host tools and Sub-Agents</b></td><td>Agents can read, write, search, execute processes, browse the web, work with attachments, and delegate to authorized Identity or Project Agents.</td></tr>
<tr><td><b>Durable Sessions</b></td><td>Session history survives restarts and supports Queueing, cancellation, Recall, policy-driven Compaction, Continuation checkpoints, automatic titles, Handoff, and Agent Takeover.</td></tr>
<tr><td><b>Skills</b></td><td>Load bundled, global, Project, Extension, and private per-Agent playbooks. Identity Agents can author private or global Skills, and users can manage them in Settings.</td></tr>
<tr><td><b>Speech, images, and embeddings</b></td><td>Bind dedicated Models for speech-to-text, text-to-speech, Image Generation/Edit, and semantic Recall independently of the Agent's chat Model.</td></tr>
<tr><td><b>Desktop voice</b></td><td>The optional Desktop app listens locally for Okay Nabu and Hey Nabu at the same time, keeps per-phrase sensitivity, supports imported TFLite wakeword models, preserves immediate command audio, and routes speech to a per-server Agent.</td></tr>
<tr><td><b>Channels and automation</b></td><td>Reach Identity Agents through Telegram and Discord, use media and commands, and schedule recurring or one-time Runs through Cron.</td></tr>
<tr><td><b>Extensions</b></td><td>Trusted local Python Extensions can add Tools, hooks, Recall backends, System Prompt blocks, settings, Channel interactions, and Skills. Home Assistant ships as a bundled Extension.</td></tr>
<tr><td><b>Observable and scriptable</b></td><td>Use Debug traces, Logs, Statistics, configuration diagnostics, HTTP RPC, per-Run SSE, and app-wide WebSocket events.</td></tr>
</table>

## Agent and Project model

An **Identity Agent** is stored under the vBot data directory and owns a Workspace, Memory, private Skills, and Sessions. It may select a registered Project as its working Project without moving any of that identity-owned state.

A **Project Agent** is a Config Agent discovered from the selected Project Source Format: `.opencode/agents/` or `.claude/agents/`. It has no independent Workspace or Memory; its runtime configuration comes from the repository plus Project defaults, capability ceilings, and vBot-owned overrides. Its Sessions live under the Project anchor and it is addressed as `agent@project`.

Project registration and Team scanning never modify the repository. Agents may still edit the repository through their normal file and shell Tools when their Run's working directory points there.

## Security

> **vBot runs AI agents with the operating-system permissions of the account that starts it.** Agents can read and write files, execute commands, edit vBot itself, contact external services, and trigger restarts. Trusted Extensions execute inside the same process. The server has no built-in authentication.

The safe default is a single-user instance bound to `127.0.0.1`. Never expose an unauthenticated vBot port to the public internet. If a Desktop Client must reach a server on another machine, keep the server on a trusted private network or VPN, restrict inbound traffic to the intended client with a firewall, and add your own authenticated TLS reverse proxy if the network boundary is not fully trusted. Binding to `0.0.0.0` makes every reachable interface part of the security boundary.

Treat network access to vBot as remote code execution on the host. Keep the data directory private because it contains credentials, OAuth tokens, Sessions, attachments, Logs, and Agent state.

## Requirements

- Python **3.11+**
- Git for installation and updates
- Node.js only when installing the current checkout or tracking `main`; release installs download a prebuilt WebUI
- A supported Provider credential, OAuth Connection, or enabled keyless local Connection before an Agent can run a Model

## Install

`scripts/install.sh` and `scripts/install.ps1` are the complete public installers. They install prerequisites when the platform supports it, select the latest release, create an isolated `~/vbot/.venv`, download the matching WebUI, install vBot, add the `vbot` command to the user PATH, enable autostart, and start the server. Runtime data stays separate under `~/.vbot`.

### Debian-like Linux / Raspberry Pi

The Installer can add missing prerequisites through `apt` on Debian-like systems, including Raspberry Pi OS:

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash
```

Pass options directly to `install.sh` after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --version v0.1.11
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --dev
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --no-autostart
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --desktop
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash -s -- --desktop-client
```

### Windows PowerShell

The no-option one-liner is:

```powershell
irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1 | iex
```

Use a ScriptBlock when passing Installer options:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Version v0.1.11
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Dev
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -NoAutostart
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -Desktop
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1))) -DesktopClient
```

Run the default Windows install from an elevated PowerShell when possible so the Task Scheduler autostart entry can be created. The immediate background server start, Task Scheduler launcher, and optional `vBot Desktop` Start-menu entry are windowless, so no persistent Python console remains after installation, appears at sign-in, or accompanies normal app launch. If Autostart setup lacks permission, the package install and immediate server start still complete; the final summary reports the missing Autostart and exact elevated recovery command, and shows a live Server URL only after the server health check succeeds. Rerunning the whole Installer is not required.

As with any `curl | bash` or `irm | iex` command, download and inspect the script first if you prefer not to execute network content directly.

## First Run

Open `http://127.0.0.1:8420/`. Runtime has already created an initial Identity Agent and its first Session. The setup guide connects a Provider or OAuth subscription and assigns a Model; creating another Agent is optional.

Provider credentials may also be added later in Settings or through the CLI. Process environment variables take precedence over the data-directory `.env` fallback.

```bash
vbot provider set-key openrouter YOUR_KEY --refresh-models
vbot provider connect openai --connection openai:subscription
vbot provider enable ollama
```

An API key passed as a CLI argument may enter shell history; Settings or a protected environment variable is preferable for real credentials.

### Install the Current Checkout

When run from inside a vBot checkout without an explicit install directory or version, the same public Installer installs that checkout into its own `.venv` and builds the WebUI locally. It never installs into the system Python environment.

**Windows:**

```powershell
.\scripts\install.ps1
.\scripts\install.ps1 -Desktop
.\scripts\install.ps1 -DesktopClient
.\scripts\install.ps1 -NoAutostart
```

**Linux:**

```bash
scripts/install.sh
scripts/install.sh --desktop
scripts/install.sh --desktop-client
scripts/install.sh --no-autostart
```

The Installer creates and uses `<checkout>/.venv`, including on PEP 668 systems. Use `--skip-webui-build` or `-SkipWebuiBuild` only when a matching `webui/dist` already exists. See [USAGE.md](USAGE.md#installation) for every Installer, install-shape, and Uninstaller option.

## Updating

```bash
vbot update
```

The updater reads the checkout-local `.vbot-install.json`, preserves the recorded install shape, Python interpreter, dependency groups, source track, and WebUI revision, and never touches the runtime data directory. Release installs move to the newest release with a matching WebUI asset; development installs pull `main` and rebuild when needed.

Tracked local changes require an explicit policy:

```bash
vbot update --stash
vbot update --discard
vbot update --no-restart
```

On Windows, updating a Desktop installation also refreshes its installer-owned Start-menu shortcut to the current windowless GUI launcher.

## Uninstalling and resetting

Run the guided removal flow:

```bash
vbot uninstall
```

It offers three explicit scopes: remove the application while keeping its data, delete only the data directory as a complete reset, or remove both. Any scope that deletes data shows the exact directory and requires `DELETE` confirmation because settings, credentials, Agents, Sessions, and all other runtime state are permanently removed. A data-only reset preserves the previous server state: a running server restarts with fresh data, while a stopped server remains stopped.

Automation must choose a scope and confirm it explicitly:

```bash
vbot uninstall --app-only --yes
vbot uninstall --data-only --yes
vbot uninstall --all --yes
```

The target defaults to the server host, port, and data directory recorded by the Installer; `--host`, `--port`, and `--data-dir` override it. Application removal deletes Autostart, launchers, and the managed environment. Windows requests elevation and completes application removal in a separate PowerShell window so the running `vbot.exe` does not lock its own environment.

## Default Data Directory

By default vBot stores runtime state under `~/.vbot`. Setup, Runtime, and managed Worktree creation all initialize the same non-destructive layout:

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
│       ├── subagents/
│       └── skill-drafts/
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

Artifacts keep their existing domain owners despite sharing `artifacts/`; Provider usage history remains Provider-owned source data despite its location under `statistics/`. The tracked system Model DB remains `resources/models/`, while only the instance's refreshable runtime Model DB lives under `artifacts/models/`. Default Workspaces remain under `agents/<agent-id>/workspace/`, and custom absolute Workspaces stay outside the data directory. `processes/` is reserved; retained Bash output lives under `artifacts/temp/bash/`.

Existing pre-layout data directories require the explicit converter before starting current code; there is no automatic migration or legacy-path fallback. See [Data directory and configuration](USAGE.md#data-directory-and-configuration) for the safe conversion sequence.

## Running from Source

For development, install the development group and WebUI dependencies:

```bash
pip install -e ".[dev]"
cd webui
npm ci
cd ..
```

Start the server in the foreground or as a managed background process:

```bash
python server/main.py
vbot server start
```

Run the Vite development server separately when working on the frontend:

```bash
cd webui
npm run dev
```

## Server Interfaces

- `POST /api/rpc` — command and management RPC
- `GET /api/runs/{run_id}/events` — complete per-Run SSE timeline
- `WS /ws` — app-wide lifecycle, reconnect, presence, and resource-change events
- `WS /ws/logs` — selected Log streaming
- `POST /api/upload` and `GET /api/attachments/{attachment_id}` — attachment transfer
- `POST /api/speech/transcribe`, `POST /api/speech/synthesize`, and `GET /api/speech/artifacts/{artifact_id}` — speech workflows
- `GET /api/images/artifacts/{artifact_id}` — generated image artifacts
- `GET /health` — exact vBot health probe

## Documentation

- [USAGE.md](USAGE.md) — installation, first-run setup, Agents, Projects, Chat, Skills, Settings, Channels, CLI, API, and troubleshooting
- [docs/extensions.md](docs/extensions.md) — trusted Python Extension authoring
- [examples/extensions](examples/extensions) — runnable Extension examples

## Quality Checks

```bash
python scripts/quality.py
python scripts/quality-frontend.py
```

The Playwright suite under `tests/e2e/` is separate and opt-in; follow the repository workflow instructions before running it.

## License

Apache-2.0 — see [LICENSE](LICENSE).
