# Agents, Projects, Sessions

Address form: a bare id (`assistant`) targets an identity agent; `agent@projekt` (e.g. `orchestrator@vbot`) targets a project agent. Accepted by `session list|create|delete`, `cron create|update`, and `prompt preview`.

## Agents

```bash
vbot agent list
vbot agent show <agent-id>
vbot agent create <agent-id> <display-name> [flags]
vbot agent update <agent-id> [flags]
vbot agent rename <current-agent-id> <new-agent-id>
vbot agent delete <agent-id>
```

Shared create/update flags: `--model`, `--fallback-model`, `--temperature <0..2>`, `--thinking-effort none|minimal|low|medium|high|xhigh|max`, `--memory-prompt-mode off|agent|agent_user`, `--custom-system-prompt true|false`, `--allowed-tools <tool> ...`, `--allowed-skills <skill> ...`, `--subagent-allow <agent> ...`, `--compaction-policy <json-object>`. Update-only: `--name`, `--clear-model`, `--clear-fallback-model`, `--clear-temperature`, `--clear-thinking-effort`, `--clear-compaction-policy`, `--current-session-id`, `--workspace <absolute-path>`, `--default-workspace`, `--copy-workspace-files`, `--project <project-id>`, `--clear-project`.

Gotchas:

- `--model`/`--fallback-model` take `<provider>/<model-id>`, optionally pinned `::<connection>[:<account>]` (e.g. `openai/gpt-5.2::api-key:work`).
- An Identity Agent may be created without `--model` so onboarding can finish before Provider setup. If neither the Agent nor global defaults supply an effective Model, the saved result warns that the Agent cannot run and prints the recovery commands. Run `vbot model list --task chat`, then `vbot agent update <agent-id> --model <model-id>` using an id from that output.
- `--allowed-tools`/`--allowed-skills` replace the whole allowlist; the flag with no values sets an empty list; quote `'*'` in shells that expand it.
- `--subagent-allow` replaces `tools.subagent.allowed_agents`; use bare Identity Agent ids or qualified `agent@project` addresses, and pass the flag with no values to deny every target.
- `--compaction-policy` replaces the full Agent Policy object. Pass JSON as one shell argument. `--clear-compaction-policy` resumes live inheritance from global Compaction settings.
- `--clear-model` and `--clear-fallback-model` remove the Agent tier so the corresponding global default can apply.
- `--clear-temperature`/`--clear-thinking-effort` drop the override so the agent inherits current defaults. `--thinking-effort none` is the literal no-reasoning value, not a clear.
- `--memory-prompt-mode` controls which Workspace memory files become prompt-visible; `--custom-system-prompt` toggles the agent's own editable prompt fragments.
- `--project` roots the Identity Agent in a registered Project: relative file and shell work uses the Project cwd, while Workspace, SOUL, Memory, Sessions, private Skills, and permissions remain the Agent's own. Use this when the user says an Agent should work in, point at, or use a Project; do not move its Workspace to the repo for that outcome.
- `--workspace` relocates only the Identity Agent's SOUL/Memory home. Use it only when the user explicitly wants those identity files stored at another path. `--copy-workspace-files` copies `SOUL.md`, `USER.md`, and `MEMORY.md` to the destination; without it, the Agent points at the destination and seeds a missing `SOUL.md`. `--default-workspace` moves it back to its data-dir home. Neither flag selects a Project.
- `--clear-project` removes the Project selection without changing Workspace or Memory.
- `rename` moves the complete Identity Agent tree and retargets live server-owned references (Channels, non-terminal Cron jobs, bare Identity Agent delegation entries, and functional Sub-Agent parent links). It preserves external custom Workspace paths and historical provenance, refuses collisions or busy old/new ids, and rolls back if a reference update fails.

```bash
vbot agent create coder Coder --model openai/gpt-5.2 --allowed-tools '*' --allowed-skills '*'
vbot agent update coder --temperature 0.4 --thinking-effort high
vbot agent update librarian --project second-brain
vbot agent rename coder researcher
```

`create` and `update` return the saved Agent, including id, Workspace, selected Project, effective Model, delegation targets, Agent Policy, effective Policy, and configuration provenance. A Workspace relocation also reports copied and backed-up files. `show` returns the same verification fields. Treat the no-effective-Model warning as incomplete setup, not a successful runnable Agent.

### Choosing the agent kind

`vbot agent create` always creates an Identity Agent with its own Workspace, `SOUL.md`, Memory, private Skills, and Sessions. When the user generically asks to create an Agent, use this kind. To make it work in a Project, create it and then root it with `agent update --project`; when requested, read the Workspace from `agent show` and customize `<workspace>/SOUL.md` with the normal file tools.

A Project Agent is different: it is a workspace-less Config Agent discovered from an existing file under the Project's selected Source Format (`.opencode/agents/` or `.claude/agents/`). It has no `SOUL.md`, Memory, or persistent identity. Do not inspect the repo or create/edit a Project Agent file merely because the user wants an Agent associated with a Project; do that only when the user explicitly asks for a Project Team member or repo-owned agent profile.

## Projects

A Project points vBot at a repo directory (its `cwd`) and exposes any agents already discovered in that repo (its Team). Adding a Project does not require a Team. vBot reads the repo but never writes it.

```bash
vbot project add <path> [--name <display-name>] [--format opencode|claude] [--default-agent <agent-id>] [--default-model <provider/model-id>] [--default-temperature <0..2>] [--default-thinking-effort <effort>] [--auto-load <file> ...] [capability flags]
vbot project list
vbot project show <project-id>
vbot project set <project-id> [--cwd <path>] [--format opencode|claude] [add flags] [--clear-default-agent] [--clear-default-model] [--clear-default-temperature] [--clear-default-thinking-effort]
vbot project set-override <project-id> <agent-id> model|temperature|thinking_effort|compaction_policy <value>
vbot project clear-override <project-id> <agent-id> model|temperature|thinking_effort|compaction_policy
vbot project rm <project-id> [--copy-rooted-agent-files]
```

- `add` needs only the repo path; everything else is optional. An empty folder is a valid project (empty team, clean report) — not an error.
- Prefer the minimal `project add <path> [--name ...]`. Do not inspect the repo first just to choose `--format`; omission uses vBot's own auto-detection. Inspect or specify the Source Format only when the user asks for it or the reported scan needs correction.
- `add` and `show` print the scan preview: the team plus a report of anything unclean (bad or unconfigured model, slug collision, unslugifiable name). `show` re-scans the repo live; `set --cwd` re-points and re-scans.
- `--format` picks the project's source ecosystem — `opencode` (`.opencode/agents/` + `.opencode/skills/`) or `claude` (`.claude/agents/` + `.claude/skills/`). Exactly one per project; agents and skills come only from that one. On `add` it is optional (omitted → auto-detected from the repo, defaulting to `opencode` when both or neither are present); on `set` it switches the format and the team + skills re-derive from the other ecosystem's directories.
- `--auto-load` lists repo files folded into project agent prompts; on `set`, the flag with no values clears the list.
- `--default-agent`/`--default-model`/`--default-temperature`/`--default-thinking-effort` are Project defaults for its Agents; the matching `--clear-*` flags remove that Project tier so resolution falls through.
- Capability flags on `add`/`set` are `--allowed-tools`, `--enabled-bundled-skills`, `--enabled-global-skills`, and `--disabled-project-skills`; each replaces its complete list, and an empty flag value clears it.
- `set-override` changes only one Project Agent's vBot-owned top-tier value; it does not edit the repo profile. `compaction_policy` takes a JSON object as one shell argument. `clear-override` removes that one field and resumes the normal Agent → Project → global chain.
- `add`, `set`, `set-override`, and `clear-override` print the saved Project plus a fresh Team/scan report, including effective Tools, repository Tool denials, overrides, and configuration-source provenance.
- `rm` archives the project's runtime anchor (never the repo) and prints the archive path. It unroots Identity Agents that selected the Project; an Agent with a custom Workspace is moved back to its default Workspace. Use `--copy-rooted-agent-files` to copy `SOUL.md`, `USER.md`, and `MEMORY.md` before that reset. The result lists affected Agents plus copied/backed-up files. Removal is blocked while a Project Agent has an active or queued Run (`project_busy`) or a Cron job targets a Project Agent (`project_in_use`) — clear those first.

## Sessions

```bash
vbot session list <agent>
vbot session create <agent> [--id <session-id>] [--make-current]
vbot session fork <agent> <session-id> [--target-agent <agent>]
vbot session rename <agent> <session-id> (--title <text> | --clear-title)
vbot session set-compaction-policy <agent> <session-id> (--policy <json-object> | --clear)
vbot session delete <agent> <session-id> --yes
vbot session link-channel <agent-id> <session-id> --channel <channel-id> --conversation <platform-conv-id>
```

- `list` shows Session ids, titles, created/last-active timestamps, the linked source Channel, and own/effective Session Policy when present.
- `create` without `--id` lets the server generate the id; `--make-current` switches the agent's active session.
- `fork` copies the complete Session into a fresh id. `--target-agent` may re-home it to another Identity or Project Agent; the result prints the new id and fork provenance.
- `rename --clear-title` restores automatic display. `set-compaction-policy --clear` resumes live Agent/global inheritance; a set result prints override, effective Policy, and source.
- `delete` requires `--yes`; the session is archived (recoverable), not erased.
- `link-channel` routes the session's outbound replies to a platform conversation (e.g. a Telegram chat id).
