---
name: vbot-docs
description: Configure and inspect vBot and its Agents. Use when a task changes or reads anything vBot holds, including your own settings.
---

# vBot

Use the `vbot` CLI through the shell Tool to read and change vBot. Without target options it reaches the server that runs you, so a saved change applies there at once, also to yourself.

Commands read `vbot <area> <action> [target] [options]`: ids are positional, everything else is a flag. `vbot help` lists the areas, `vbot <area>` its actions, `vbot <area> <action> --help` the exact arguments. Use only commands and flags that help shows.

## Do the task

1. Read the reference for the task from the table below. Done when you know its commands and which flags replace a whole value.
2. Inspect the target with its `list` or `show` command and reuse the exact ids it prints. Before you replace a Skill, a prompt block or an automation prompt, read its complete content; list output shortens it.
3. Change only what the request names. Omitted update flags leave a field unchanged; list and policy flags, such as `--allow`, `--tool-allow` or `--sources`, replace the whole value, so repeat every entry to keep. The references name the commands that clear omitted options.
4. Read the result before you report. `[OK]` means the command finished, `[WARN]` names pending work or a limit, `[ERROR]` a failure. A saved setting is not yet working behavior: an Agent without an effective Model cannot run, an enabled Channel can fail to connect, an accepted update or restart has not finished. Report what the output shows.

For a question about a past conversation, use `session_search` first; read `references/session-search.md` only when its excerpts lack the evidence or you need exact wording, a Session list or a Tool Result.

| Task | Read |
|---|---|
| Create an Agent for a purpose, rename an Agent, Agent settings, Tool and Skill permissions, delegation | `references/agents.md` |
| Write or revise an Agent's SOUL.md: its role, how it works, its voice, its boundaries | `references/soul.md` |
| Projects and their Agents, Sessions (create, fork, rename, delete), archived Agents, Projects and Sessions | `references/projects-sessions.md` |
| Send a message to an Agent and read its answer, continue one of its Sessions, set a Session's Model, thinking effort or temperature, or help the user open the terminal TUI | `references/chat.md` |
| Past conversations beyond `session_search`: listing Sessions, full transcripts, exact Tool Results | `references/session-search.md` |
| Provider keys/OAuth/limits, Models, voices, specialized Task Models, and image, video or music generation settings such as quality, image count or video length | `references/providers.md` |
| Settings, System Prompt blocks, Extension settings and enabling or disabling an Extension | `references/configuration.md` |
| Write or change an Extension: its Tools, hooks, Commands, settings | `references/extensions.md`; for a page or its own Sessions, then `references/extension-pages.md` |
| Use bundled Swarm or Computer Use | `references/extension-usage.md` |
| MCP installation, Tool access, discovery and application operations | `references/mcp.md` |
| Channels (Telegram, Discord, Slack, Mattermost, WhatsApp): first setup, tokens, allowed chats, group access | `references/channels.md` |
| Recurring or one-time scheduled Runs | `references/cron.md` |
| Runs after startup, including restart continuation | `references/bootstrap.md` |
| Install a Skill from a link, archive or folder; inspect, author, share, disable or pin Skills; review their history; restore deleted Skills; Librarian passes over an Agent's own Skills | `references/skills.md` |
| Pinned Memory for an Identity Agent, its change history, restoring earlier entries | `references/memory.md` |
| Logs, Provider traces, Session usage statistics and server performance | `references/diagnostics.md` |
| Server start/stop/restart, update, uninstall, Autostart, Desktop, Doctor, access mode and paired devices, reaching another vBot server and signing in to it | `references/server.md` |
| Named credentials and the Secret Store, database health, data snapshots, configuration backups and recovery | `references/data-store.md` |
| Agent Secrets that Agents use as `{{secret:NAME}}`: which Agents may use them and where, waiting Secret requests, Secret Sources such as password managers | `references/agent-secrets.md` |
| Filesystem investigation, data location, backups or manual repair | `references/system-layout.md` |

When no row fits, or the reference you read lacks the answer, search the `references` folder for the command, flag or term with `search_files`, its `path` set to that folder inside the directory the `skill` Tool reported when it loaded this Skill. Then read the file that matches.

## Inputs, credentials and failures

- Pass multiline content with `--file` and JSON arrays or objects as one argument; when the shell breaks the quoting, pipe the value to the command's stdin option. Edit vBot's files by hand only when no command can make the change, then run `vbot doctor config`.
- Pass a credential only on the stdin of the command made for it; vBot keeps it in its encrypted Secret Store and ignores `.env` files. Never put a credential in an argument, a file or a reply. When the user has not given you the value, give them `vbot data-store secrets set <NAME>` or the area's stdin command to run in their terminal; never ask for the value or offer to take it in the chat, because the chat history keeps it.
- On `[ERROR]`, the output explains the failure and prints `Next:` commands to inspect or recover; vBot never runs them itself. Inspect the target before you repeat a change, because earlier steps of a failed command can already be saved.
