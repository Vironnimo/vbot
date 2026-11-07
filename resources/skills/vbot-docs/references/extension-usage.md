# Using bundled Extensions

Read the section of the requested Extension: [Software Factory](#software-factory), [Swarm](#swarm), [Computer Use](#computer-use), or [KeePassXC and 1Password](#keepassxc-and-1password). MCP connections have their own reference, [mcp.md](mcp.md); creating an Extension is in [extensions.md](extensions.md), its settings and enable/disable in [configuration.md](configuration.md#extensions).

## Software Factory

Software Factory runs work Cards through a Project's Workflows - plans, drafts, changes, reviews and integration, each done by an Agent you choose - and the user accepts the result. It is bundled as `factory`; the `Software Factory` page holds the Board and its `Waiting for you` review inbox.

In Setups, give a registered Project one or more Workflows, starting from templates such as Implement, Plan, Plan from drafts, or Plan, then implement, and pick the default. A Workflow is a list of steps; each step has a name, an Agent (any of your Agents or a member of the Project's Team, with optional Model and thinking effort overrides), a task, and when it starts. Steps run one at a time in list order. A step receives the results of the steps it comes after, and of theirs, and nothing else, so drafts that come after nothing stay independent; a step can also wait for the user's approval before it starts. A step with a send-back target can return that step's result with findings instead of passing it on. The setup also holds the turn budget per participant.

The Library holds shared templates for one step, a sequence of steps, or a complete Workflow. Each publication creates a version; Projects keep the version they adopted and their local adjustments until an update is applied there. An update compares the adopted version, the Project's current work and the chosen newer version, preserving local adjustments and exposing conflicting changes for a choice. Archiving a template hides it from normal Library browsing without removing published versions or existing uses.

- The Factory does no Git. Participants work in isolation, commit, merge and clean up as the Project's instructions describe; the Implement templates end with an Integrate step that waits for the user's approval and lands the reviewed change.
- A Card runs the Workflow chosen at creation (`factory_card` `workflow`, or the Project's default); running Cards keep the Workflow they started with. Cards the user creates on the page or through management operations queue automatically; `factory_card` queues direct user requests and currently authorized automations, while an Agent's own idea stays a Proposal until the user approves it.
- A step with a send-back target runs as a fresh participant each time. Its findings return to the target step's Session, and every step after the target runs again. Its rounds limit (default 2) is the number of automatic send-backs; past it, or when a participant is blocked, the Card waits for the user with the result and the reason.
- When the last step has handed in, the Card waits for the user. Accept starts a step waiting for approval, overrules a step that exhausted its rounds, or finishes the Card. Send back supplies a comment and can pick the step to rework, Answer resolves a blocked question, Retry resumes a failed or interrupted step, and Discard stops the running step. Neither Send back nor Discard reverts anything a participant already changed.
- The Extension settings `wip_limit` (default 2) and `max_waiting` (default 0, unlimited) control admission; waiting Cards do not occupy running slots, and accepted work waits in Backlog when all slots are busy.
- Waiting Cards and Proposals produce a WebUI/tray notification and contribute to the page badge; the Board, not the transient toast, keeps the durable record.
- Permission requests stay in vBot's normal approval UI; open the linked Session from the Card rather than changing Permission Rules or granting the request yourself.

Page actions and scripts share management operations:

```bash
vbot extensions operations factory
vbot extensions run factory setup.update --help
vbot extensions run factory board.get
vbot extensions run factory card.create --help
vbot extensions run factory card.get --help
vbot extensions run factory library.list
vbot extensions run factory library.publish --help
vbot extensions run factory library.insert --help
vbot extensions run factory library.preview_update --help
```

Use Card and Project ids from those reads. Deleting a final Card archives its participant Sessions; done and discarded Cards otherwise keep readable Sessions on the Board.

For Library work, use the template id and version from `library.list` or `library.get`. `library.extract` prepares a whole Workflow or selected steps for publication; when revising an existing template, pass its `source_id` to retain the original step identities. `library.publish` creates a template or another version, with the template's current `expected_revision` when updating it. After publishing from a Project, use `library.link` to connect its existing steps to that version without inserting copies. To replace existing links with a newly published template, pass `replace_sources: true`; the selection must cover every surviving step of each overlapping use.

Inserting, linking or updating a Library template returns a Workflow draft; save it through `setup.update` to change the Project. Keep the setup's other Workflows and pass the setup's `expected_revision` from `setup.get`. To adopt an update, pass the Workflow and its source id to `library.preview_update`, choose local or template values for conflicts, then call `library.apply_update` with the exact version previewed and those choices before saving. These operations use the same implementation as the Library page; their `--help` lists the arguments, and `--stdin` accepts a JSON argument object without shell quoting.

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
