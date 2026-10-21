# Using bundled Extensions

Read the section of the requested Extension: [Swarm](#swarm), [Computer Use](#computer-use), or [KeePassXC and 1Password](#keepassxc-and-1password). MCP connections have their own reference, [mcp.md](mcp.md); creating an Extension is in [extensions.md](extensions.md), its settings and enable/disable in [configuration.md](configuration.md#extensions).

## Swarm

A Swarm is a group of participant Agents, each with its own Session, that work on one goal as peers and coordinate on a shared Board and Wiki. A profile fixes the participants' Models and counts, the Project or working directory, their Tools and an optional Compaction Policy. The user manages profiles and Swarms on the `Swarms` page; `/swarm <profile-slug> "<goal>"` starts one from a chat.

You manage them with the Extension's operations; each prints its arguments with `--help`:

```bash
vbot extensions operations swarm
vbot extensions run swarm catalog
vbot extensions run swarm profiles.list
vbot extensions run swarm profiles.get --help
vbot extensions run swarm swarms.start --help
```

- `catalog` lists the Models, Tools, Skills and Projects a profile can use. Read a profile with `profiles.get` before `profiles.save`, which replaces it and needs its `expected_revision`.
- `swarms.start` starts a profile with a goal; the participants receive only the goal, no Session history or attachments of yours. The goal is pinned on the Board, and the participants discuss it there before they start the work.
- `swarms.get` shows a Swarm's state and participants. A participant's status comes from its Runs: `idle`, `running`, `failed`, `cancelled` or `interrupted`. When every participant is idle, the Swarm rests without Model calls; it is not finished.
- `board.read` and `board.post` read and write the Board, `wiki` the Wiki, `swarms.usage` the token usage.
- `swarms.stop` stops the Runs and keeps Sessions, Board and Wiki; `swarms.resume` continues inactive participants in their Sessions, also one after a failed Run. `swarms.delete` permanently removes a stopped Swarm with its Board, Wiki and participant Sessions; run it only when the user asks. The profile stays.
- Profile changes apply to Swarms started afterwards; `swarms.settings` changes the delivery settings of a running Swarm.

## Computer Use

The bundled `computer_use` Extension lets an Agent operate the Windows desktop of the vBot server's computer with the user's real mouse and keyboard. It works when the server runs on Windows in a signed-in desktop session and needs no installation. Its three opt-in Tools are `computer` (screenshots, zoom, clicks, typing, keys), `computer_batch` (several actions in one call) and `computer_apps` (list apps and displays, bring an app to the front, request approval, hand the computer back). Enable them in the Agent's Tool settings; a Project Agent also needs them in the Project's `--allowed-tools` and its Tool override. The Extension's `computer-use` Skill teaches how to work with them.

By default, an Agent with these Tools operates every application without asking. The Extension setting `ask_per_app` ("Ask before each app") makes the user approve applications per Session instead: the Agent asks with `computer_apps` `request`, the user answers in the vBot app's request dialog, and the Agent waits up to five minutes. In that mode, screenshots show unapproved apps as gray boxes, input goes only to approved apps, and approvals end 30 minutes after the Session's last Computer Use call or when the server restarts or the Extension reloads. Applications running as administrator receive no input in either mode.

While an Agent controls the desktop, an orange frame lines the screens, from its first desktop call until it hands the computer back with `computer_apps` `release`, its reply ends, or two minutes pass without a call. The user stops it at any time with the Stop control in the Chat composer or by pressing Esc twice within 600 ms; the Agent cannot take the computer again until the user's next message. Input already delivered cannot be undone.

## KeePassXC and 1Password

The bundled `keepassxc` and `onepassword` Extensions make entries of the user's password manager a source for Agent Secrets; they add no Tools. The user binds a Secret to an entry and unlocks the source in Settings -> Secrets, which also shows whether the source is unavailable, locked or available, and why.

- KeePassXC reads one local database through `keepassxc-cli`, which comes with KeePassXC. Set the database file, `key_file` only when the database has a key file, and `cli_path` only when `keepassxc-cli` is neither on PATH nor in the KeePassXC installation folder:
  ```bash
  vbot extensions set keepassxc database "C:\Users\<user>\Documents\Passwords.kdbx"
  ```
  Replace the path with the user's database. The master password has no command: the user enters it at each unlock in Settings -> Secrets, and vBot keeps it in memory until the source is locked or the server restarts.
- 1Password reads the vaults of a 1Password service account through the 1Password CLI `op`, which must be installed on the server's computer. The user creates the service account in their 1Password account with read access to the vaults vBot needs, then saves its token in their terminal with `vbot extensions set onepassword token --stdin` or enters it at each unlock in Settings -> Secrets.