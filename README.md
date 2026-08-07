<p align="center">
  <img src="webui/public/brand/vbot-mark-transparent.png" alt="vBot logo" width="120">
</p>

<h1 align="center">vBot</h1>

<p align="center"><strong>Your Agents. Your machine. Your Models.</strong></p>

<p align="center">Run persistent AI Agents on your own hardware, connect the Providers you choose, and reach them from the WebUI, Desktop, CLI, Telegram, or Discord.</p>

<p align="center">
  <a href="https://github.com/Vironnimo/vbot/releases"><img src="https://img.shields.io/github/v/release/Vironnimo/vbot?style=flat-square" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/status-alpha-orange?style=flat-square" alt="Status: Alpha">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Raspberry%20Pi-555?style=flat-square" alt="Windows, Linux, and Raspberry Pi">
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="USAGE.md">User guide</a> ·
  <a href="#security">Security</a> ·
  <a href="docs/extensions.md">Extension development</a>
</p>

vBot is a local-first, self-hosted home for personal and project Agents. An Agent can keep durable Memory and Sessions, work with files and processes through Tools, use reusable Skills, delegate to Sub-Agents, and run on a schedule. Projects can also discover an existing OpenCode or Claude Code team directly from a repository without copying it into vBot.

## Get started

The public Installer selects the latest release, creates an isolated environment, downloads the prebuilt WebUI, adds the `vbot` command, enables Autostart, and starts the server. Runtime data stays separate under `~/.vbot`, and a normal release installation does not require Node.js.

### Windows

Open a normal, non-elevated PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.ps1 | iex
```

### Debian-like Linux and Raspberry Pi

The Linux Installer can add missing prerequisites through `apt`:

```bash
curl -fsSL https://raw.githubusercontent.com/Vironnimo/vbot/main/scripts/install.sh | bash
```

### First Run

1. Wait for the Installer to report that vBot is ready.
2. Open [http://127.0.0.1:8420/](http://127.0.0.1:8420/).
3. Follow the setup guide to connect a Provider or OAuth subscription and choose a Model, then send the first message in the Session that vBot created for you.

The WebUI is the easiest place to configure vBot. The Installer also supports Desktop, remote Desktop Client, development, custom port, and no-Autostart installations; see the complete [Installation guide](USAGE.md#installation) when the default path is not the one you need.

If you prefer not to execute downloaded shell content directly, inspect [install.ps1](scripts/install.ps1) or [install.sh](scripts/install.sh), download the appropriate file, and run it locally.

## What you get

| Capability | What it means for you |
|---|---|
| Persistent personal Agents | Identity Agents keep their own Workspace, Memory, private Skills, permissions, and Sessions across restarts. |
| Repository-native teams | Projects discover OpenCode or Claude Code Project Agents in place, while a personal Agent can work in a Project without giving up its identity. |
| Provider and Model choice | Connect OpenAI, Anthropic, OpenRouter, Ollama, Mistral, GitHub Copilot, and compatible endpoints through API keys, OAuth, or keyless local Connections. |
| Real host capabilities | Agents can work with files, processes, the web, attachments, speech, images, Extensions, and authorized Sub-Agents through validated Tools. |
| Available where you are | Use the same vBot installation through the WebUI, optional Desktop app, CLI, Telegram, Discord, and HTTP API. |
| Long-running workflows | Queue messages, schedule recurring or one-time Runs, recover interrupted work, search older Sessions, and inspect Logs, Statistics, and Debug traces. |

## A simple operating model

- **Identity Agents** are persistent personal Agents. Each has its own Workspace, Memory, Skills, permissions, and Sessions.
- **Projects** register repositories on the server. They can supply repository-defined Project Agents and Project Skills without vBot modifying the repository during registration or discovery.
- **Providers** connect vBot to hosted or local Models. Each Agent can use its own Model, fallback, and specialized Models for speech, images, or semantic Recall.
- **WebUI, Desktop, CLI, and Channels** are different ways to use the same vBot installation. Closing one of them does not stop the server or an active Run.

## Security

> **vBot Agents run with the operating-system permissions of the account that starts the server.** They can read and write files, execute commands, edit vBot itself, contact external services, and trigger restarts. Trusted Extensions run inside the same process.

The server has no built-in authentication and binds to `127.0.0.1` by default. Never expose an unauthenticated vBot port to the public internet. Use a trusted private network or VPN, restrictive firewall rules, and an authenticated TLS reverse proxy when remote access is required.

Treat network access to vBot as remote code execution on the host. Keep `~/.vbot` private because it contains credentials, OAuth tokens, Sessions, attachments, Logs, and Agent state. See [Operational notes](USAGE.md#operational-notes) for the complete deployment guidance.

## Everyday lifecycle

Update an Installer-managed installation:

```bash
vbot update
```

Run the guided removal or reset flow:

```bash
vbot uninstall
```

Both commands preserve the distinction between the application and its runtime data. The Uninstaller shows the exact target and requires explicit confirmation before deleting data. See [Updating and uninstalling](USAGE.md#updating-and-uninstalling) for recovery options and automation flags.

## Documentation

| Goal | Start here |
|---|---|
| Install or choose a different install shape | [Installation](USAGE.md#installation) |
| Connect a Provider and select a Model | [First-run setup](USAGE.md#first-run-setup) |
| Use Agents, Projects, Sessions, Chat, and the Queue | [Agents, Projects, and Sessions](USAGE.md#agents-projects-and-sessions) · [Chat, Queue, and Built-in Commands](USAGE.md#chat-queue-and-built-in-commands) |
| Configure Skills, Tools, Models, Channels, or schedules | [Skills, Tools, and Sub-Agents](USAGE.md#skills-tools-and-sub-agents) · [Settings and specialized Models](USAGE.md#settings-and-specialized-models) · [Channels](USAGE.md#channels) · [Cron](USAGE.md#cron) |
| Operate or integrate the server | [Running the server](USAGE.md#running-the-server) · [CLI reference](USAGE.md#cli-reference) · [Server API](USAGE.md#server-api) |
| Build an Extension | [Extension authoring guide](docs/extensions.md) · [Runnable examples](examples/extensions) |
| Develop and verify vBot itself | [Development and verification](USAGE.md#development-and-verification) |

## Project status

vBot is alpha software. Back up `~/.vbot` before upgrades or manual configuration changes, expect contracts to evolve between releases, and report reproducible problems through [GitHub Issues](https://github.com/Vironnimo/vbot/issues).

## License

Apache-2.0 — see [LICENSE](LICENSE).
