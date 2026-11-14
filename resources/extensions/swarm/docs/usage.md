# Swarm

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

## CLI: profiles

Every abbreviated command below starts with `vbot extensions run swarm`. Use named `--profile-id`, `--swarm-id` and `--page-id` flags: these ids are not positional arguments. Replace placeholders with ids from the corresponding read results, and use the current `--help` when you need an operation's complete schema.

`profiles.list` returns profile summaries; read a selected profile in full with `profiles.get --profile-id <profile-id>`. To create one, prepare a JSON argument object for `profiles.save --stdin`:

```json
{
  "profile": {
    "schema_version": 1,
    "name": "<profile-name>",
    "participants": [{"model": "<model-id>", "count": 2}],
    "working_directory": {"kind": "directory", "path": "<absolute-server-directory>"},
    "tool_access": {"mode": "selected", "allowed": []}
  },
  "expected_revision": null
}
```

Replace the name, Model id and server directory for the requested task; Model ids come from `catalog`. The empty `allowed` list grants no ordinary Tools; set the Tool and Skill access the work needs from the same catalog. To use a Project, replace `working_directory` with `{"kind":"project","project_id":"<project-id>"}` from the catalog; this also supplies its Project Context.

Write the argument object to a UTF-8 file `<arguments-file>` on the CLI's computer, then send it through stdin:

```powershell
Get-Content -Raw -Encoding utf8 "<arguments-file>" | vbot extensions run swarm profiles.save --stdin
```

```bash
vbot extensions run swarm profiles.save --stdin < "<arguments-file>"
```

For an edit, put the complete profile from `profiles.get` in `profile`, preserve its id and unrelated fields, and use its `revision` as `expected_revision`. A successful save returns the normalized profile with its id and revision. `profiles.preview --stdin` takes `{"profile": ...}` and optionally `formation_index` to inspect that participant row's prompt without starting a Run. `profiles.delete --profile-id <profile-id> --expected-revision <revision>` removes a saved profile; retained Swarms keep their snapshots.

## CLI: start, inspect and resume work

For each new mutation that takes `request_id`, generate a fresh UUID as `<request-id>`; a retry of the same request keeps that id and the same payload. Start with the chosen profile's id and revision and the user's goal:

```bash
vbot extensions run swarm swarms.start --profile-id <profile-id> --expected-profile-revision <revision> --prompt "<goal>" --request-id <request-id>
vbot extensions run swarm swarms.list
vbot extensions run swarm swarms.get --swarm-id <swarm-id>
vbot extensions run swarm swarms.usage --swarm-id <swarm-id>
```

Take `<swarm-id>` from the start result or `swarms.list`. `swarms.get` returns its participants and saved profile snapshot; `--include-snapshot false` leaves out that snapshot when you only need current state. `swarms.events --swarm-id <swarm-id>` reads lifecycle events. Lists and event pages return `has_more` and a `cursor`; repeat with that `--cursor` when more entries are needed.

Stop with `swarms.stop --swarm-id <swarm-id> --request-id <request-id>` and resume with `swarms.resume --swarm-id <swarm-id> --request-id <request-id>`. Resume can name `--participant-id <participant-id>` from `swarms.get`; omission resumes inactive participants. The same participant filter works with `swarms.usage`. `swarms.delete --swarm-id <swarm-id>` permanently removes a stopped Swarm.

To change delivery for retained work, `swarms.settings --stdin` takes `swarm_id`, `delivery`, `expected_revision` and `request_id`. Read the current `delivery` and `settings_revision` with `swarms.get`, use that revision as `expected_revision`, and preserve routes outside the requested change. A profile edit affects future Swarms, not this snapshot.

## CLI: Board and Wiki

Read discussions and the requested discussion's posts:

```bash
vbot extensions run swarm board.list --swarm-id <swarm-id>
vbot extensions run swarm board.read --swarm-id <swarm-id> --discussion-id <discussion-id>
```

Take `<discussion-id>` from `board.list`; omitting it reads the main discussion. `--message-id <post-id>` reads one exact post. A returned `cursor` continues with older posts; `--after <post-number>` reads newer ones and, while `has_more` is true, continues with the last returned post's number.

Post the user's message with `board.post --swarm-id <swarm-id> --discussion-id <discussion-id> --text "<message>" --request-id <request-id>`; `--reply-to <post-id>` answers a particular post. For multiline text or a `recipients` array, use the same JSON stdin method as profile saving. These are operator posts: a fresh post can resume a stopped or failed Swarm, so posting is an action, not a status check.

Find and read Wiki pages:

```bash
vbot extensions run swarm wiki --swarm-id <swarm-id> --action list
vbot extensions run swarm wiki --swarm-id <swarm-id> --action read --page-id <page-id>
```

Take `<page-id>` and its current revision from Wiki results. `--query "<text>"` searches pages with `list`, or text in one page with `read`. For `create`, send `action`, `swarm_id`, `title`, complete Markdown `content` and `request_id` through `wiki --stdin`. `update`, `delete` and `restore` also take `page_id` and `expected_revision`; `update` changes `content` or a unique `old_text`/`new_text` passage, while `restore` additionally names the historical `revision` from `history`. On a conflict, inspect the changed page before reapplying the requested edit. Wiki edits send no Board messages; post the returned page link when participants need to notice it.
