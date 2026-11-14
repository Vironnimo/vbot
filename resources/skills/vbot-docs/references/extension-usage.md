# Using bundled Extensions

Read the section of the requested Extension: [Software Factory](#software-factory), [Swarm](#swarm), [Computer Use](#computer-use), or [KeePassXC and 1Password](#keepassxc-and-1password). MCP connections have their own reference, [mcp.md](mcp.md); creating an Extension is in [extensions.md](extensions.md), its settings and enable/disable in [configuration.md](configuration.md#extensions).

## Software Factory

Software Factory runs work Cards through Workflows - plans, drafts, changes, reviews and integration, each done by an Agent you choose - and the user accepts the result. It is bundled as `factory`; the `Software Factory` page holds the Board with its `Waiting for you` review inbox, and the Workflows page.

A Workflow is global and can fix a Project: then all its Cards work there and its steps can use the Project's Team. Without one, each Card names its Project or has none, and then works in a folder of its own. Start from templates such as One Agent, Review pushes, Implement, Plan or Plan from drafts. Each step has a name, an Agent (any of your Agents, or a member of the Project's Team, with optional Model and thinking effort overrides) and a task, or it starts another Workflow as a new Card and can wait until that Card is done. A step starts once the steps it comes after are done, so steps without a common predecessor run in parallel. A step receives the results of the steps it comes after, and of theirs, and nothing else. A step can wait for the user's approval before it starts, and the Workflow can wait for the user's acceptance when all steps are done. A step with a send-back target can return that step's result with findings instead of passing it on.

Triggers create Cards for a Workflow by themselves. A schedule takes a cron expression such as `0 9 * * 1-5`, `every 2h`, `in 30m` or an ISO time. A webhook gets a URL and a secret; a GitHub webhook (content type `application/json`, the secret, events such as push) creates one Card per delivery with the event's details, and its Project can be the Project whose Git remote matches the repository. GitHub must reach the vBot address.

Workflows and their Steps can independently limit Runs, Tool calls, input plus output Tokens and Model cost in USD; blank or 0 means unlimited, the default. A Step gets a new budget on rework; the Card keeps its total. Pause and Retry retain consumption. The Agent receives a budget notice near the limit to finish or report progress and the additional budget needed. The first reached limit pauses that Step; a running Model request can overshoot a Token or cost limit. On a waiting Card, change its limits and Retry the Step to resume. Through management, `card.get` provides its revision and consumption per Step; `card.limits` takes that `expected_revision` and replacements for `card_limits`, or `step_limits` together with `step`. Omit a scope to keep it, or pass `{}` to remove its limits.

- The Factory does no Git. Participants work in isolation, commit, merge and clean up as the Project's instructions describe; the Implement template ends with an Integrate step that waits for the user's approval and lands the reviewed change.
- A Card runs the Workflow chosen at creation (`factory_card` `workflow`, or the first enabled Workflow); running Cards keep the Workflow they started with. Cards the user creates on the page or through management operations, and Cards from triggers, queue automatically; `factory_card` queues direct user requests and currently authorized automations, while an Agent's own idea stays a Proposal until the user approves it.
- A step with a send-back target runs as a fresh participant each time. Its findings return to the target step's Session, and every step after the target runs again. Its rounds limit (default 2) is the number of automatic send-backs; past it, or when a participant is blocked, that step waits for the user with the result and the reason while parallel steps continue.
- Each waiting step has its own actions: Accept starts a step waiting for approval or overrules a step that exhausted its rounds, Answer resolves a blocked question, Retry resumes a failed, interrupted or paused step. Accept on the Card finishes it when it waits for acceptance. Send back supplies a comment and can pick the step to rework, and Discard stops the Card and the Cards its steps wait for. Neither Send back nor Discard reverts anything a participant already changed.
- The Extension settings `wip_limit` (default 2) and `max_waiting` (default 0, unlimited) control admission; waiting Cards do not occupy running slots, and accepted work waits in Backlog when all slots are busy.
- Waiting Cards and Proposals produce a WebUI/tray notification and contribute to the page badge; the Board, not the transient toast, keeps the durable record.
- Permission requests stay in vBot's normal approval UI; open the linked Session from the Card rather than changing Permission Rules or granting the request yourself.

Page actions and scripts share management operations; each prints its arguments with `--help`, and `--stdin` accepts a JSON argument object without shell quoting:

```bash
vbot extensions operations factory
vbot extensions run factory workflow.list
vbot extensions run factory workflow.save --help
vbot extensions run factory board.get
vbot extensions run factory card.create --help
vbot extensions run factory card.get --help
```

Use Card, Workflow and Project ids from those reads. `workflow.save` replaces the whole Workflow: read it with `workflow.get` first and pass its `expected_revision`; omit `expected_revision` to create one. `workflow.secret` shows a webhook's secret for the sender's settings. Deleting a final Card archives its participant Sessions; done and discarded Cards otherwise keep readable Sessions on the Board.

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
