# vBot System and Data Layout

Read this reference when a task requires locating vBot state, searching runtime files, diagnosing missing or corrupt state, or reasoning about a backup, move, reset, or unavoidable manual edit.

## Mental model

The server owns one async Runtime and its data directory. WebUI, Desktop, CLI, and Channels are Accessors to that Runtime; they do not maintain independent copies of Agents, Projects, Sessions, or Settings. Except for the local lifecycle, installation, path, and Doctor commands identified in `SKILL.md`, the CLI talks to the server through RPC.

An Identity Agent is a stored configuration with a Workspace and Sessions. A Project is a stored reference to an external cwd plus its Project configuration and repo-discovered Team. A Session is durable conversation history owned by one Agent. A Run is one active execution inside a Session; Queue and active-Run state are in memory, while the resulting conversation events are appended to the Session.

## Locate the correct instance

Run:

```bash
vbot home [--data-dir <path>]
```

Use its absolute `app_dir` and `data_dir` results. Do not assume `~/.vbot`: development checkouts, worktrees, environment overrides, and explicit `--data-dir` targets may resolve elsewhere.

`vbot home` is local and takes no `--host` or `--port`. It reports the installation and data root on the machine where the command executes, not the data directory of a remote server selected by other CLI commands. When the CLI targets a remote server, use RPC-backed list/show/status/diagnostic commands unless the task also has filesystem access on that server host.

Keep the four path roles distinct:

| Path | Ownership and purpose |
|---|---|
| `app_dir` | vBot code, bundled resources, and bundled Skills |
| `data_dir` | One server instance's Settings, credentials, Identity Agents, Sessions, artifacts, and operational state |
| Project `cwd` | External repository or working folder used by Project file/shell Tools |
| Agent Workspace | Identity and Memory files; inside the Agent data home by default, but optionally external |

Use `vbot project show <project-id>` to resolve a Project cwd and `vbot agent show <agent-id>` to resolve an Identity Agent Workspace. Path equality between a Workspace and cwd does not connect their ownership or behavior.

## Data directory map

Some directories are created only when their owning feature first writes data.

| Relative path under `data_dir` | Contents and owner | Primary inspection or mutation route |
|---|---|---|
| `settings.json` | Internal persisted Settings document | Use `vbot config list/describe/get/effective`; use `raw` and `vbot doctor settings` only for diagnosis |
| `.env` | Sensitive fallback credentials; the process environment has higher precedence | Use Provider, Channel, or Extension credential commands; never print the file or expose values |
| `agents/<agent-id>/agent.json` | Identity Agent configuration | `vbot agent show/list/update` |
| `agents/<agent-id>/workspace/` | Default Workspace with `SOUL.md`, `USER.md`, and `MEMORY.md`; the configured Workspace may instead be external | `vbot agent show`; use the Memory and file Tools according to their ownership |
| `agents/<agent-id>/sessions/<session-id>.jsonl` | Identity Agent Session transcript, with adjacent metadata/activity sidecars when present | `vbot session list` and Session commands; treat live transcript files as system-owned append stores |
| `agents/<agent-id>/skills/` | Skills private to one Identity Agent | `vbot skill read/create/update/delete --scope agent:<agent-id>` |
| `agents/<agent-id>/prompts/` | Agent-scoped System Prompt layout and overrides when custom prompting is enabled | `vbot prompt ... --scope agent:<agent-id>` |
| `projects/<project-id>/project.json` | Project record, including the external cwd and Project policy | `vbot project show/list/set` |
| `projects/<project-id>/agents/<agent-id>/sessions/` | Project-scoped Config Agent Sessions | `vbot session ...` with the qualified `agent@project` address |
| `skills/` | User-global Skills shared across Identity Agents, subject to their policy | `vbot skill ... --scope global` |
| `prompts/` | Default-scope System Prompt layout and overrides | `vbot prompt ... --scope default` |
| `channels/<channel-id>/` | Channel configuration plus Channel-owned routing/idempotency state | `vbot channel list/status/update`; credentials remain outside Channel JSON |
| `cron/` | Cron jobs and scheduler-owned once-fire claims | `vbot cron list/create/update/delete` |
| `attachments/` | Durable uploaded/downloaded blobs with JSON sidecars | Resolve through Session/attachment behavior; do not infer content from filename extensions alone |
| `speech/` and `images/` | Durable speech and generated-image artifacts with metadata sidecars | Use the task result and serving interfaces; these are not temporary files |
| `models/` | Complete runtime Model DB published by Model refresh | `vbot model list/refresh`; do not hand-edit generated catalogs |
| `recall/` | Derived lexical/vector Session recall indexes | Use recall behavior and rebuild mechanisms; canonical history remains in Sessions |
| `logs/` | Daily server/kernel application logs | `vbot log list/read` |
| `debug/` | Debug trace index and redacted raw Provider traffic under `traces/` | `vbot debug status/traces/trace/clear` |
| `oauth/` | Sensitive OAuth token state | Provider connect/status/disconnect commands; never print or copy tokens into chat |
| `extensions/` | User-installed single-file or package Extensions and optional bundled Extension Skills | `vbot extensions list/reload/enable/disable`; additional configured roots may live elsewhere |
| `archive/` | System-owned archived Agent, Project, and Session trees created by destructive lifecycle operations | Inspect only to understand or recover an archived resource; do not treat it as active state |
| `temp/bash/` and `temp/subagents/` | Retained diagnostic output with category-specific expiry | Inspect when a Tool points to a retained file; do not treat it as durable application state |
| `.tmp/` | Short-lived atomic-write and refresh staging | Never use as a source of truth |

Project Skills do not live in the data directory: they stay in the Project cwd under the directory selected by its Source Format (`.opencode/skills/` or `.claude/skills/`). Bundled Skills live under `app_dir/resources/skills/`. Configured extra Skill and Extension directories may also be external.

## Search and edit discipline

1. Resolve the target instance and path role before searching.
2. Use the owning CLI list/show/status command first; it understands identifiers, validation, and live state better than a filesystem scan.
3. Search the narrowest owning directory using a known Agent, Project, Session, Channel, trace, or artifact id. Do not recursively dump the whole data directory: it contains credentials, OAuth tokens, private conversations, and attachments.
4. Treat Session JSONL, recall indexes, Model DB files, OAuth state, `.tmp`, Queue state, and active Runtime files as system-owned. Inspect read-only unless a documented recovery procedure explicitly requires otherwise.
5. Prefer CLI mutations. If a user-editable JSON file had to be changed manually, stop concurrent writes where appropriate, preserve a backup, and run `vbot doctor config` before restart or further mutation.
6. Verify the result through the same owning CLI area rather than assuming a successful filesystem write changed live Runtime state.
