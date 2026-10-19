# vBot System and Data Layout

This covers where vBot keeps its state on the server's computer, for finding files, diagnosing missing or damaged state, and backups, moves, resets or a manual edit. The server holds all state; WebUI, Desktop, CLI and Channels keep no copies of their own.

## Locate the instance

`vbot home [--data-dir <path>]` prints the absolute `vbot_root` and `data_dir` of the computer where you run it. Use those paths; development checkouts, worktrees, `VBOT_DATA_DIR` and `--data-dir` resolve elsewhere than `~/.vbot`. For a remote server, use its list, show and status commands unless you also have file access on that computer.

Keep the four path roles distinct:

| Path | Ownership and purpose |
|---|---|
| `vbot_root` | vBot code, bundled resources, and bundled Skills |
| `data_dir` | One server instance's Settings, credentials, Identity Agents, Sessions, artifacts, and operational state |
| Project `cwd` | External repository or working folder used by Project file/shell Tools |
| Agent Workspace | Identity and Memory files; inside the Agent's data home by default, or elsewhere |

Use `vbot project show <project-id>` to resolve a Project cwd and `vbot agent show <agent-id>` to resolve an Identity Agent Workspace. Path equality between a Workspace and cwd does not connect their ownership or behavior.

## Data directory map

Some directories are created only when their owning feature first writes data.

| Relative path under `data_dir` | Contents and owner | Primary inspection or mutation route |
|---|---|---|
| `settings.json` | Internal persisted Settings document | Use `vbot config list/describe/get/effective`; use `raw` and `vbot doctor settings` only for diagnosis |
| `secrets.json` | The encrypted Secret Store with every saved credential and OAuth login; its key lives outside the data directory, and a variable of the same name in the server's process environment takes precedence | `vbot data-store secrets list/set/unset` or the Provider, Channel and Extension credential commands; never edit or print the file |
| `agents/<agent-id>/agent.json` | Identity Agent configuration | `vbot agent show/list/update` |
| `agents/<agent-id>/workspace/` | Default Workspace with `SOUL.md`, `USER.md`, and `MEMORY.md`; the configured Workspace can instead be external | `vbot agent show`; use the Memory and file Tools according to their ownership |
| `sessions.db`, `channels.db`, `provider-usage.db`, `model-usage.db` and `data-store.json` | Canonical SQLite databases: Session history (Project and Identity Sessions, archived ones included), Channel state (conversation routing, group access, delivery receipts), Provider limit samples and recorded Model usage; `data-store.json` authorizes every canonical database and records its identity | Use `vbot session ...` for Sessions and `vbot data-store ...` for health, snapshots, recovery, and incident acknowledgement; read `sessions.db` only read-only as `session-search.md` describes; never write or copy these files |
| `agents/<agent-id>/skills/` | Skills private to one Identity Agent | `vbot skill read/create/update/delete --scope agent:<agent-id>` |
| `agents/<agent-id>/prompts/` | Agent-scoped System Prompt layout and overrides when custom prompting is enabled | `vbot prompt ... --scope agent:<agent-id>` |
| `projects/<project-id>/project.json` | Project record, including the external cwd and Project policy | `vbot project show/list/set` |
| `extension-data/<extension-id>/` | Extension-owned data: canonical Extension databases (`*.db`, registered in `data-store.json`) and files such as the MCP Extension's `connections.json` | Managed by the owning Extension; for a removed Extension, `vbot data-store unregister ext.<extension-id>.<name> --yes` releases its database, only when the user asks (`data-store.md`) |
| `snapshots/`, `incidents/`, `quarantine/` and `config-backups/` | Verified data snapshots of the canonical databases and JSON documents, recovery incidents, damaged database files or replaced JSON documents kept as evidence, and the automatic configuration backups | `vbot data-store ...` |
| `skills/` | User-global Skills shared across Identity Agents, subject to their policy | `vbot skill ... --scope global` |
| `prompts/` | Default-scope System Prompt layout and overrides | `vbot prompt ... --scope default` |
| `channels/<channel-id>/` | Channel configuration `channel.json`; a WhatsApp Channel also keeps its bridge installation and pairing files here. Routing and delivery state live in `channels.db` | `vbot channel list/status/update`; credentials remain outside Channel JSON |
| `cron/` | Cron jobs and their fire records | `vbot cron list/create/update/delete` |
| `calendar/` | Calendar events | the `calendar` Tool |
| `bootstrap/` | Startup-triggered Agent Run jobs | `vbot bootstrap list/create/update/delete` |
| `artifacts/attachments/` | Durable uploaded/downloaded blobs with JSON sidecars | Resolve through Session/attachment behavior; do not infer content from filename extensions alone |
| `artifacts/speech/` | Durable generated speech artifacts | Use the returned artifact reference; generated images default to the caller’s Workspace or Project cwd under `image-gen/` |
| `artifacts/models/` | Complete runtime Model DB published by Model refresh | `vbot model list/refresh`; do not hand-edit generated catalogs |
| `recall/` | Derived lexical/vector Session recall indexes | Use recall behavior and rebuild mechanisms; canonical history remains in Sessions |
| `logs/` | Daily server/kernel application logs | `vbot log list/read` |
| `artifacts/debug/` | Debug trace index and redacted raw Provider traffic under `traces/` | `vbot debug status/traces/trace/clear` |
| `artifacts/performance/` | Performance recordings: a timeline trace plus a summary per recording; the newest 20 are kept | `vbot performance recordings/record`; do not hand-edit |
| `.env`, `oauth/` | Plaintext credential files, which vBot ignores until `vbot data-store secrets convert --yes` imports them into the Secret Store | `vbot data-store secrets status` names them and what to do; never print them or copy their values |
| `access/` | The server's recorded access mode (`server.json`) and paired devices (`devices.json`) | `vbot application access show` and `vbot devices list`; never edit these files: deleting `devices.json` signs out every device |
| `extensions/` | User-installed single-file or package Extensions and optional bundled Extension Skills | `vbot extensions list/reload/enable/disable`; the Settings path `extensions.directories` adds roots elsewhere |
| `archive/` | System-owned files of archive entries (`archive/entries/<entry-id>/`, older entries also under `archive/agents/` and `archive/projects/`); their Sessions stay in `sessions.db`, and entries are deleted permanently after the retention period | Use `vbot archive list/show/restore/purge`; never move or delete files here by hand |
| `pre-generation-1/` | Present only in a data directory converted from the data format of vBot 0.4.4 or earlier: every file the conversion replaced, including old copies of credential files, plus `conversion-report.json`; vBot never reads it | Do not read credentials from it or restore files from it; deleting it is the user's decision |
| `artifacts/temp/commands/` and `artifacts/temp/subagents/` | Retained diagnostic output with category-specific expiry | Inspect when a Tool points to a retained file; do not treat it as durable application state |
| `artifacts/temp/atomic/` | Short-lived atomic-write and refresh staging | Never use as a source of truth |

Project Skills stay in the Project cwd under its enabled Skill Sources, including `.agents/skills/`. Read `project show <project-id>` to see the selected Sources. Bundled Skills live under `vbot_root/resources/skills/`. Configured extra Skill and Extension directories can also be external.

## Search and edit discipline

1. Resolve the instance and the path role before searching.
2. Use the owning CLI list, show or status command first; it knows the ids and the live state that a file scan misses.
3. Search the narrowest owning directory using a known Agent, Project, Session, Channel, trace, or artifact id. Do not recursively dump the whole data directory: it contains credentials, OAuth tokens, private conversations, and attachments.
4. Treat `sessions.db` and other canonical databases, `data-store.json`, data snapshots, recovery incidents and quarantine bundles, recall indexes, Model DB files, the Secret Store, atomic staging, Queue state, and active Runtime files as system-owned. Inspect read-only unless a documented recovery procedure explicitly requires otherwise.
5. Prefer CLI mutations. If a user-editable JSON file had to be changed manually, stop concurrent writes where appropriate, preserve a backup, and run `vbot doctor config` before restart or further mutation.
6. Verify the result through the same owning CLI area rather than assuming a successful filesystem write changed live Runtime state.
