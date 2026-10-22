# Permission Rules

The optional rules that confine what an Agent's Tools may read, change, run and fetch, and the Permission Boundary that answers every such check for one Run.

## Overview

`core/permissions/` (`permissions.py`, re-exported by `core.permissions`) owns the rule language (parse, normalize, validate, per-part resolution), placeholder expansion, path normalization, the `PermissionBoundary` and its decisions, the protected locations, the refusal type and its Agent-facing text, the presets and editor data, and the command check (`_command_check.py::CommandChecker`). `commands/` holds the shell command analysis (References). Apart from the checker, which owns the analyzer, the package is a pure library: it keeps no global state and learns every folder it protects or grants from the `BoundaryContext` its caller builds; at import it loads only the standard library and `core.utils.errors`.

Not owned here: storing `permission_rules` on Agents, Settings defaults, Project overrides and `TemporaryAgentConfig` (their owners call `normalize_permission_rules`), resolving them per part (`projects/resolution.md` -> Permission Rules), building the Run's boundary and carrying it to `ToolContext` (`chat/run-execution.md` -> Permission Boundary), the checks inside each Tool (Tools), persisting ceilings on Sub-Agent Sessions and Cron jobs (`subagents/delegation.md`, `automation/cron.md`), and every UI. `tool_failure_for_exception` maps `PermissionDenied` to `permission_denied` (`tools/registry-dispatch.md` -> Failure codes).

## Requirements (user decisions 2026-10-21)

1. Rules are optional and configured explicitly per Agent; without them an Agent has full access, as before. Choosing a Project for a Session confines nothing.
2. Threat model: mistakes of well-meaning Agents, and Models that must stay out of certain areas. Not a defense against a hostile or prompt-injected Agent.
3. No OS sandbox, no approvals: vBot enforces the rules itself. A later OS backend must be able to enforce the same rule model unchanged.
4. The shell is confined "in a sensible way", not only the file Tools.
5. Rule-based configuration like Claude Code and OpenCode, with presets as starting points; built polished from the start.
6. An Agent can change only files it can read. A stored rule set with `write: anywhere` beside `read: work_area` is refused; when per-part resolution combines them from different levels, writing narrows to `work_area` (`write_within_read`). A `Read` deny also refuses changes and an `Edit` allow also permits reading, so no Tool result can leak a file's content to an Agent that may change but not read it.
7. Full access overrides a restrictive shared default: the Full access preset stores the explicit full set. A set that restricts nothing is full access and has no protected locations, so such Agents keep managing vBot. No own rules ("use the default") is a separate choice.
8. A restricted Agent cannot loosen its own rules: its `agent.json` is protected for reading and writing, and the repository files a Team member's rules are translated from are protected for writing.
9. A restricted Agent reads only its own Session's temporary files in the data directory (command and terminal logs, screenshots, speech, Sub-Agent activity): they live in per-Session folders, and the boundary grants only the Run's own (`session_artifact_dirs`, `storage.md`).

Honest limits, stated wherever "restricted" is shown: programs a command starts act with the server user's full rights and are not inspected inside; terminal input is checked only at an integrated shell prompt (`tools/terminal.md` -> Permission Rules); the local browser's redirect hops and the subresources of a page are not checked against web rules; MCP servers and Extensions are outside the checks; hardlinks to outside files are not detected; git can show files from its history, and git work across a folder or repository (`git pull`, `git restore .`, `git add .`) can read and change denied files and permission settings files below it (`permissions/commands.md` -> Paths); an older vBot ignores the rules after a downgrade.

## Terms

Shared term: GLOSSARY -> Permission Rules.

### Work Area
The folders a restricted Agent may write by default: its working directory and its Scratch Area. `read: work_area` reads there plus read allow rules. Not the Workspace, which lies in the data directory and is readable but writable only when it is the working directory.

### Permission Boundary
The effective, placeholder-free rules of one Run: the Agent's own resolved rules expanded for this Run, plus inherited ceilings, plus the protected locations. Every check asks it; `PermissionBoundary.UNRESTRICTED` is full access.

### Ceiling
A resolved, placeholder-free rule set inherited from whoever caused the Run (the Session that started a Sub-Agent, the Session that created a Cron job, an Extension). The Run may do only what every layer permits.

### Layer
One placeholder-free rule set inside a boundary (`PermissionLayer`), tagged with its `source`: `agent` (the Agent's own rules), `parent`, `cron` or `extension`.

## Interfaces

Rule language, presets, resolution, descriptions and editor data: `permissions/rule-language.md` (`normalize_permission_rules` validates every stored form, `resolve_rule_set` resolves per part, `PermissionRules.restricts` decides full access).

**Boundary:**
- `PermissionBoundary.build(rules, context, *, ceilings=()) -> PermissionBoundary`; `BoundaryContext(workdir, home, data_dir, project_root, scratch_root, workspace, agent_config, rule_files, install_dirs, readable_dirs, loopback_port, own_hosts, platform)`. A rule whose placeholder has no value in this Run is dropped.
- `check_path(path, access, *, base=None, subtree=False, glob=None, pruned=(), git=False, deny_only=False)` with `access` `read`, `write` or `start_dir` (the directory a process starts in, decided like a read); recursive and wildcard accesses, `refusable_below` and `readable_entries`: `permissions/paths.md`. `check_host(host, access="web"|"connect")` (`connect`: hosts ssh, scp and `git@host:` reach); `check_url(url)` (http/https only; refuses the vBot server's own port on every host `is_server_host` names, `permissions/rule-language.md` -> Bases, kinds and placeholders). Each returns `None` or a `PermissionRefusal`.
- For command checks: `restricted`, `layers` (`PermissionLayer.commands`, `shell_allow`, `shell_deny`), `context`, `loopback_port`, `refusal(...)` (builds a refusal with the allowed summary) and `allowed_summary(access)`.
- `to_ceiling_dict(*, source)` / `ceiling(*, source)` turn the boundary into a ceiling for a Run it causes (its own layer retagged as `source`, inherited layers keep theirs); `PermissionCeiling.from_ceiling_dict(value)` reads one strictly (exact fields, every base present, absolute placeholder-free path patterns).

**Command checks** (`CommandChecker`, one per Runtime, `runtime._command_checker`): `check_line(boundary, command_line, *, shell, cwd, env, home, workdir_parameter=False)` for a shell Tool call (whose cwd hint names `workdir`), a terminal line or a Cron watch command; `check_argv(boundary, argv, *, cwd, env, home)` for a program a terminal starts. Each returns `None` or a `PermissionRefusal`. A full-access boundary returns at once; the first restricted check creates the `CommandAnalyzer` (`permissions/commands.md` -> PowerShell helper). `aclose()` (shutdown step `command_analysis`, `runtime/lifecycle-steps.md`) stops it; later checks refuse. Analysis and decision run on bounded worker pools, off the Event Loop. A failed analysis refuses (fail closed, `retryable`); every unresolved element refuses too (`unanalyzable`, with a fix in `hint`): with the protected locations, no unresolved path or command is harmless. Decision order: `permissions/commands.md` -> Command checks.

**Refusal:** `PermissionRefusal(access, target, reason, source, rule, base, location, detail, resolved, allowed, hint, below, retryable)`; `reason` is `deny_rule` (`below`: the denied part a recursive or wildcard access would reach), `protected_location` (`location` `data_dir`, `installation`, `agent_config`, `vbot_server`, `vbot_cli`), `not_permitted` (by `base` of layer `source`), `unsafe_form` (`location` names the form) or `unanalyzable` (`detail` is the caller's clause). `hint` names the way forward instead of the closing "tell the user"; with it, and for every `unanalyzable` refusal, the message leaves out that every other way is refused too. `retryable`: only a failed analysis. `refusal.message(tool=...)` is the Agent-facing text, `refusal.watch_message()` the wording for a Cron watch check, which also reaches the user's alert; `PermissionDenied(refusal)` raises it through a Tool.

**Users** (where the library is wired in):
- Stored rule sets: `agent.json` `permission_rules` (`agent/configuration.md`), `defaults.agent.permission_rules` (`settings.md`), the Project override `overrides.<agent>.permission_rules` (`projects/configuration.md`), `AgentProfile.permission_rules` of a repository profile (`projects/scanning.md` -> Permission Rules translation) and `TemporaryAgentConfig.permission_rules` / `.permission_ceiling` (`agent/temporary-agents.md`). Each owner normalizes on write; a stored set that does not normalize, an unknown part included, makes its document invalid and unwritable, never unrestricted.
- Resolution: `RuntimeAgent.permission_rules` (`None` or a set, which may restrict nothing) and `RuntimeAgent.permission_ceiling` (temporary Agents); provenance in `effective_config(...)["permission_rules"]` (`projects/resolution.md` -> Permission Rules).
- Run: `core/chat/_run_permissions.py::session_permissions` composes every Session boundary (Run, `permissions.session`, Cron watch checks), rebuilt before a Tool round once a rule source changed; Tools read `ToolContext.permissions`. `ToolContext.resolve_path(..., access=)` checks `read`, `write` and `start_dir` and raises `PermissionDenied` (`tools/file_state.md` -> Path resolution).
- Paths beyond the Tool target: listings, missing-path suggestions, diagnostics, search roots and exclusions, image, media and Channel files, the `project` Tool's instruction files, MCP roots and Assistant `file:` references are checked before any access, so no refused path is touched or named beyond a readable folder's entries and deny rules (`tools/file_state.md` -> Path resolution, `chat/request-building.md` for file references, `extensions/mcp.md` for roots).
- Shell: `bash`/`powershell` check every call before launch and run it in a confined environment (`tools/shell.md` -> Permission Rules).
- Cron watch checks of a job with a ceiling: the command and URL at every check and when the `cron` Tool saves the watch (`automation/cron-watch.md`).
- Web: `web_fetch` at the address, every redirect hop and final page; `analyze_image` image URLs and hops (`fetch_public(..., permissions=)`); `web_search` drops results it may not fetch (`tools/web_fetch.md`, `tools/web_search.md` -> Permission Rules).
- Terminal: `start` (program, folder), lines submitted at an integrated shell prompt, and `attach`/`status` only for terminals started under the same or narrower rules (`tools/terminal.md` -> Permission Rules).
- Other Sessions: `session_search` searches only the current Agent's Sessions (`tools/session_search.md`).
- Ceilings: `AgentOverrides.permission_ceiling` of a Session, joined and never widened (`source` `parent` by delegation and `send`, `cron` for a fired Session), `CronJob.permission_ceiling` (`source` `cron`, `CronJob.ceiling()`), `TemporaryAgentConfig.permission_ceiling` (`source` `extension`).
- Editors and summaries: RPC `agent.create`/`agent.update` `permission_rules`, `permissions.validate`, `permissions.session` and `permissions.clear_session_ceiling` (`server/session-agent-rpcs.md` -> Permission Rules), the CLI permission flags of `agent` and `project override set` (`cli/agent-commands.md`), the WebUI editors, the Session `Restricted` badge and refused Tool rows (`webui/settings.md`, `webui/projects.md`, `webui/chat.md`). `permissions_block_text(boundary)` gives the text the Agent reads.

## Decision order

For a path, in this order (normalization, unsafe forms and links in detail: `permissions/paths.md`):
1. Normalize the path; refuse unsafe forms (network, device, alternate-stream and drive-relative paths) unless an allow rule without wildcards names them.
2. Resolve links and junctions; the lexical and the resolved form must both pass every later step.
3. Protected locations: the Agent's `agent.json` (always); the files a Team member's rules come from and install and interpreter dirs (writes); the data directory, except the Workspace, the Scratch Area and `readable_dirs` (the Run's own artifact folders and Skill folders) for reading, and the Scratch Area or a Workspace that is the working directory for writing.
4. A deny rule of any layer refuses: `Read` deny covers reads and writes, `Edit` deny writes.
5. Every layer must permit, by its base or an allow rule: an `Edit` allow covers reads too. The Workspace and `readable_dirs` count as readable in every layer.

Hosts and commands: a deny rule of any layer refuses; under `listed` every such layer needs an allow rule. Host patterns: `permissions/rule-language.md` -> Bases, kinds and placeholders.

## Constraints & Gotchas

- `UNRESTRICTED` (every boundary without layers) answers each check before any work; keep that the first statement of every new check: full-access Agents pay nothing.
- A path pattern covers what it names and everything below it, with or without wildcards; Tool paths expand only `~`, never placeholders (`permissions/paths.md`).
- `limits_writes` (a narrowed write base or a path deny rule in any layer) decides changes no path rule covers: `skill_manage` then changes only the Agent's own Skills (`skills/skill-manage.md` -> Calls).
- Ceilings are self-contained: they store their own Work Area folders, so a Sub-Agent working elsewhere stays inside its parent's area.

## Agent-facing text

This domain owns the refusal texts (`PermissionRefusal.message` and `watch_message` for Cron watch checks, `core/permissions/_refusal.py`), the command-check reasons and hints (`_command_check.py`), and the reasons for the `core:permissions` System Prompt block and the permission Run failures that Chat renders (`core/chat/_run_permissions.py`). Before you change, add or delete a sentence of them, read `permissions/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## Tests

`tests/core/permissions/`: rule language, resolution and presets (`test_permission_rules.py`), boundary decisions, protected locations, ceilings, hosts and URLs (`test_permission_boundary.py`), paths (`test_permission_paths.py`), command checks (`test_command_check.py` over `command_check_support.FakeAnalyzer`, which the Tools' tests use too). The failure code: `tools/registry-dispatch.md`.

## References

Read these only when your task matches - not by default.

- Changing how shell command lines are analyzed or checked (CommandPlan, PowerShell helper, path tiers, cwd tracking, Shell(...) patterns, the checker's decision order) -> `permissions/commands.md`
- Changing the stored rule form, its validation, placeholders, host patterns, resolution, presets, layer descriptions, or what `permissions.validate` gives the editors (`editor_bases`) -> `permissions/rule-language.md`
- Changing how a path is normalized or matched (unsafe forms, links and junctions, subtree and glob accesses, `refusable_below`, `readable_entries`, case and platform differences) -> `permissions/paths.md`
- Changing, adding or deleting a sentence of a refusal, a command-check reason or hint, the Cron watch refusal, the `core:permissions` System Prompt block or a permission Run failure -> `permissions/agent-facing-text.md`
