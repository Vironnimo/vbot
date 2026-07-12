# Agents, Projects, Sessions

Address form: a bare id (`assistant`) targets an identity agent; `agent@projekt` (e.g. `orchestrator@vbot`) targets a project agent. Accepted by `session list|create|delete`, `cron create|update`, and `prompt preview`.

## Agents

```bash
vbot agent list
vbot agent show <agent-id>
vbot agent create <agent-id> <display-name> [flags]
vbot agent update <agent-id> [flags]
vbot agent delete <agent-id>
```

Shared create/update flags: `--model`, `--fallback-model`, `--temperature <0..2>`, `--thinking-effort none|minimal|low|medium|high|xhigh|max`, `--memory-prompt-mode off|agent|agent_user`, `--custom-system-prompt true|false`, `--allowed-tools <tool> ...`, `--allowed-skills <skill> ...`. Update-only: `--name`, `--clear-temperature`, `--clear-thinking-effort`, `--current-session-id`.

Gotchas:

- `--model`/`--fallback-model` take `<provider>/<model-id>`, optionally pinned `::<connection>[:<account>]` (e.g. `openai/gpt-5.2::api-key:work`).
- `--allowed-tools`/`--allowed-skills` replace the whole allowlist; the flag with no values sets an empty list; quote `'*'` in shells that expand it.
- `--clear-temperature`/`--clear-thinking-effort` drop the override so the agent inherits current defaults. `--thinking-effort none` is the literal no-reasoning value, not a clear.
- `--memory-prompt-mode` controls which workspace memory files become prompt-visible; `--custom-system-prompt` toggles the agent's own editable prompt fragments.
- Workspace paths are not mutable through the CLI.

```bash
vbot agent create coder Coder --model openai/gpt-5.2 --allowed-tools '*' --allowed-skills '*'
vbot agent update coder --temperature 0.4 --thinking-effort high
```

## Projects

A project points vBot at a repo directory (its `cwd`) and exposes the agents discovered in that repo (its team). vBot reads the repo but never writes it.

```bash
vbot project add <path> [--name <display-name>] [--format opencode|claude] [--default-agent <agent-id>] [--default-model <provider/model-id>] [--default-temperature <0..2>] [--default-thinking-effort <effort>] [--auto-load <file> ...]
vbot project list
vbot project show <project-id>
vbot project set <project-id> [--cwd <path>] [--format opencode|claude] [add flags] [--clear-default-agent] [--clear-default-model] [--clear-default-temperature] [--clear-default-thinking-effort]
vbot project rm <project-id>
```

- `add` needs only the repo path; everything else is optional. An empty folder is a valid project (empty team, clean report) — not an error.
- `add` and `show` print the scan preview: the team plus a report of anything unclean (bad or unconfigured model, slug collision, unslugifiable name). `show` re-scans the repo live; `set --cwd` re-points and re-scans.
- `--format` picks the project's source ecosystem — `opencode` (`.opencode/agents/` + `.opencode/skills/`) or `claude` (`.claude/agents/` + `.claude/skills/`). Exactly one per project; agents and skills come only from that one. On `add` it is optional (omitted → auto-detected from the repo, defaulting to `opencode` when both or neither are present); on `set` it switches the format and the team + skills re-derive from the other ecosystem's directories.
- `--auto-load` lists repo files folded into project agent prompts; on `set`, the flag with no values clears the list.
- `--default-agent`/`--default-model`/`--default-temperature`/`--default-thinking-effort` are Project defaults for its Agents; the matching `--clear-*` flags remove that Project tier so resolution falls through.
- `rm` archives the project's runtime anchor (never the repo) and prints the archive path. It is blocked while a project agent has an active or queued run (`project_busy`) or a cron job targets a project agent (`project_in_use`) — clear those first.

## Sessions

```bash
vbot session list <agent>
vbot session create <agent> [--id <session-id>] [--make-current]
vbot session delete <agent> <session-id> --yes
vbot session link-channel <agent-id> <session-id> --channel <channel-id> --conversation <platform-conv-id>
```

- `list` shows session ids, created/last-active timestamps, and the linked source channel when one exists.
- `create` without `--id` lets the server generate the id; `--make-current` switches the agent's active session.
- `delete` requires `--yes`; the session is archived (recoverable), not erased.
- `link-channel` routes the session's outbound replies to a platform conversation (e.g. a Telegram chat id).
