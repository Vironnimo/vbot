# Projects, Sessions, Archive

Address form: a bare id (`assistant`) targets an Identity Agent; `agent@project` (`orchestrator@vbot`) targets a Project Agent.

## Projects

A Project points vBot at a repository directory (its `cwd`) and exposes the Agents defined in that repository (its Team). A Project needs no Team. vBot reads the repository and never writes it.

```bash
vbot project add <path> [--name <display-name>] [--sources <json-array>] [--model-mappings <json-object>] [--default-agent <agent-id>] [--default-model <provider/model-id>] [--default-temperature <0..2>] [--default-top-p <0..1>] [--default-thinking-effort <effort>] [--auto-instructions on|off] [--folder-instructions on|off] [--auto-load <file>...] [capability flags]
vbot project list
vbot project show <project-id>
vbot project set <project-id> [--cwd <path>] [add flags] [--clear-default-agent] [--clear-default-model] [--clear-default-temperature] [--clear-default-top-p] [--clear-default-thinking-effort]
vbot project override set <project-id> <agent-id> model|temperature|top_p|thinking_effort|compaction_policy|tool_access|tool_loading|permission_rules <value>
vbot project override clear <project-id> <agent-id> model|temperature|top_p|thinking_effort|compaction_policy|tool_access|tool_loading|permission_rules
vbot project detect [<path>]
vbot project remove <project-id> [--copy-rooted-agent-files] [--permanent --yes]
```

- Paths are on the server's computer; use absolute paths when the CLI runs elsewhere. `detect` without a path inspects the server's working directory.
- Add a Project with `project add <path> [--name ...]`; vBot detects the repository's Agent and Skill Sources itself. An empty folder is a valid Project with an empty Team. `add`, `show` and `set --cwd` scan the repository and print the Team with a report of anything unclean: a bad or unconfigured Model, a name collision, a name that gives no usable id.
- `--sources` replaces the complete ordered list, for example `[{"id":"claude.agents","enabled":true},{"id":"shared.skills","enabled":true}]`. Copy Source ids from `project detect <path>` or `project show`. Earlier enabled Sources win name collisions; shadowed definitions stay in the report. Newly detected Sources are enabled unless they collide with an enabled Source. A Source entry can carry `agent_paths`, which limits it to those Agent definitions; keep the field when you change switches or order, and remove it to include every detected definition.
- `--model-mappings` replaces the whole mapping from the Model names that repository Agents ask for to vBot Models, for example `{"sonnet":"anthropic/claude-sonnet-4-6"}`; targets must be usable vBot Models. An unmapped name uses the Project or global default and is reported; `inherit` uses the caller's Model when delegated, otherwise the default.
- A Team member's status: `ready` means every reported setting was applied or translated, `limited` that some are unsupported, `needs_attention` that the definition cannot run. Exact Tool lists, including `[]`, stay exact. Unsupported command, path, approval or isolation restrictions disable the affected Tools; a vBot Tool override can replace them within the Project ceiling. External hooks and MCP servers from the repository are not started.
- The Project's prompts load the repository's main instruction file on their own: `AGENTS.md`, else the first of `CLAUDE.md`, `.claude/CLAUDE.md`, `GEMINI.md` and `.github/copilot-instructions.md`; `show` names it after `auto_instructions`. `--auto-instructions off` stops that. `--auto-load` lists additional files loaded after it; on `set`, the flag without values clears the list. With `--folder-instructions on`, the default, an Agent that works in a subfolder receives that folder's `AGENTS.md`, `CLAUDE.md` or `GEMINI.md`.
- `--default-agent`, `--default-model`, `--default-temperature`, `--default-top-p` and `--default-thinking-effort` are defaults for the Project's Agents; each `--clear-*` flag removes one so the global default applies.
- The capability flags `--allowed-tools`, `--enabled-bundled-skills`, `--enabled-global-skills` and `--disabled-project-skills` each replace their complete list; the flag without values clears it.
- `override set` changes one Project Agent's value in vBot without editing the repository. `compaction_policy` and `tool_access` take a JSON object as one argument. A Tool override replaces the repository's Tool policy but stays inside the Project's `--allowed-tools` ceiling: `'{"mode":"selected","allowed":["read"]}'` selects only `read` when the Project permits it. `tool_loading` takes the On-demand Tools setting of `agent update --tools-on-demand` as a JSON object, `'{"on_demand":true}'` or `'{"on_demand":true,"always_loaded":["read","bash"]}'`; without it a Project Agent loads no Tools on demand. `permission_rules` takes the Permission Rules of `agent update --permissions`, a preset name (`work-dir`, `work-dir-read-only`) or a JSON object; each part it sets replaces the rules vBot translated from the repository's settings. `override clear` removes the one value again.
- `add`, `set` and both `override` commands print the saved Project and a fresh Team report with effective Tools, repository Tool denials, overrides and where each value comes from.
- `remove` archives the Project's vBot record and its Sessions, never the repository, as one archive entry and prints its id; `--permanent --yes` deletes them at once. It clears the default Project of the Identity Agents that use it and moves an Agent with a custom Workspace back to its default Workspace; `--copy-rooted-agent-files` first copies their SOUL.md, USER.md and MEMORY.md. The result lists those Agents (`default_project_cleared_for`) and the copied files. Identity Agent Sessions that work in the Project stay readable but cannot run until the Project is restored. `remove` refuses while a Project Agent has an active or queued Run (`project_busy`) or a Cron or Bootstrap job targets one (`project_in_use`); clear those first.

## Sessions

```bash
vbot session list <agent>
vbot session create <agent> [--id <session-id>] [--make-current]
vbot session fork <agent> <session-id> [--target-agent <agent>]
vbot session rename <agent> <session-id> (--title <text> | --clear-title)
vbot session policy set <agent> <session-id> (--policy <json-object> | --clear)
vbot session delete <agent> <session-id> --yes [--permanent]
vbot session channel link <agent-id> <session-id> --channel <channel-id> --conversation <platform-conv-id>
```

- `list` prints up to 100 Sessions: ids, titles, created and last-active times, the linked Channel, and the Session's own and effective Compaction Policy. `--limit <count>` sets the page size and `--cursor <json-object>` from the result continues; `--all` fetches every page. Keep the same Agent and server target. To read a Session's Messages, read `references/session-search.md`.
- `create` without `--id` lets the server choose the id; `--make-current` makes it the Agent's current Session.
- `fork` copies the complete Session under a new id; `--target-agent` gives the copy to another Identity or Project Agent. The result prints the new id.
- `rename --clear-title` returns to the automatic title. `policy set` needs the complete Compaction Policy object with `enabled`, `trigger` and `strategy`: read it from `list` and keep its other fields; `--clear` makes the Session follow the Agent and global settings again.
- `delete` requires `--yes` and moves the Session into the archive; `--permanent` deletes it at once.
- `channel link` sends the Session's replies to a platform conversation, such as a Telegram chat id.

## Archive

```bash
vbot archive list [--kind agent|project|session|owner_group|files] [--agent <agent-id>] [--project <project-id>]
vbot archive show <entry-id>
vbot archive restore <entry-id> [--as <new-id>]
vbot archive purge <entry-id>... --yes
vbot archive purge --all [--kind ...] [--agent <agent-id>] [--project <project-id>] --yes
```

- Deleting an Agent, Project or Session creates an archive entry (`arc_...`), restored or deleted as a whole; archiving the same id again creates another entry.
- Entries are deleted permanently once the retention period since their archiving has passed (`archive.retention_days`, default 30; `null` keeps them until deleted). A changed period applies to existing entries at once: lowering it with `vbot config set` deletes the entries that are now older right away. `list` names the period in its header and each entry's `purge_at`. `purge_at=-` means no automatic deletion is scheduled: the period is off, the row is `kind=files`, `user_folders=yes` or `origin=recovered`, its deletion is under way (`state=purging`), or the header says automatic deletion is paused because vBot cannot read the period (`vbot doctor settings` shows why).
- Run `show` before a restore: it lists the entry's Sessions and files and whether a restore is possible.
- `restore` brings back the Agent, Project or Session with its Sessions, delegation grants and default Projects; Identity Agent Sessions that worked in a restored Project can run there again. When the id is in use, it refuses and names the conflict; repeat it with `--as <new-id>`. Entries of kind `owner_group` (Sessions of an Extension) and `files` cannot be restored.
- `purge` deletes entries permanently. Recorded usage stays in Statistics; uploaded attachments are not deleted. `purge --all` never deletes entries that can hold the user's folders (`files` entries and rows marked `user_folders=yes`); it lists them as `kept`, and only `purge <entry-id>... --yes` deletes them.
