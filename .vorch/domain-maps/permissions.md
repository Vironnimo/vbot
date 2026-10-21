# Permission Rules

The optional rules that confine what an Agent's Tools may read, change, run and fetch, and the Permission Boundary that answers every such check for one Run.

## Overview

`core/permissions/` (`permissions.py`, re-exported by `core.permissions`) owns the rule language (parse, normalize, validate, per-part resolution), placeholder expansion, path normalization, the `PermissionBoundary` and its decisions, the protected locations, the refusal type and its Agent-facing text, the presets and the per-layer descriptions editors and Sessions show, and the command check (`_command_check.py::CommandChecker`). `commands/` holds the shell command analysis (References). Apart from the checker, which owns the analyzer, the package is a pure library: it keeps no global state and learns every folder it protects or grants from the `BoundaryContext` its caller builds; at import it loads only the standard library and `core.utils.errors` (`idna` and `ctypes` load where first used).

Not owned here: storing `permission_rules` on Agents, Settings defaults, Project overrides and `TemporaryAgentConfig` (their owners call `normalize_permission_rules`), resolving them per part (`projects/resolution.md` -> Permission Rules), building the Run's boundary and carrying it to `ToolContext` (`chat/run-execution.md` -> Permission Boundary), the checks inside each Tool (Tools), persisting ceilings on Sub-Agent Sessions and Cron jobs (`subagents/delegation.md`, `automation/cron.md`), and every UI. `tool_failure_for_exception` maps `PermissionDenied` to `permission_denied` (`tools/registry-dispatch.md` -> Failure codes).

## Requirements (user decisions 2026-10-21)

1. Rules are optional and configured explicitly per Agent; without them an Agent has full access, as before. Choosing a Project for a Session confines nothing.
2. Threat model: mistakes of well-meaning Agents, and Models that must stay out of certain areas. Not a defense against a hostile or prompt-injected Agent.
3. No OS sandbox, no approvals: vBot enforces the rules itself. A later OS backend must be able to enforce the same rule model unchanged.
4. The shell is confined "in a sensible way", not only the file Tools.
5. Rule-based configuration like Claude Code and OpenCode, with presets as starting points; built polished from the start.
6. An Agent can change only files it can read. A stored rule set with `write: anywhere` beside `read: work_area` is refused; when per-part resolution combines them from different levels, writing narrows to `work_area` (`write_within_read`). So no Tool result can leak a file's content to an Agent that may change but not read it.
7. Full access overrides a restrictive shared default: the Full access preset stores the explicit full set. A set that restricts nothing has no protected locations, so such Agents keep managing vBot. No own rules ("use the default") is a separate choice.

Honest limits, stated wherever "restricted" is shown: programs a command starts act with the server user's full rights and are not inspected inside; terminal input is checked only at an integrated shell prompt, input typed ahead while a program runs reaches the shell unchecked once that program ends, and a program can print fake prompt and folder marks; the local browser's redirect hops and the subresources of a page are not checked against web rules; MCP servers and Extensions are outside the checks; hardlinks to outside files are not detected; an older vBot ignores the rules after a downgrade.

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

**Rule language** (stored form `{read, write, commands, web, allow, deny}`, every part optional):
- `normalize_permission_rules(value, *, notes=None, platform=sys.platform) -> PermissionRules`: the one strict validator for every stored form; raises `PermissionRulesError` (a `ValueError`) naming the part, the value and what is accepted. Unknown parts are refused. `None` parts count as not set. Rule strings rewritten from another harness's spelling (aliases, bare `Bash`, `:*`, `domain:`, a URL, Claude Code's `//path`) add a sentence to `notes`; separator and whitespace normalization is silent.
- `normalize_permission_rule(text) -> RuleNormalization(rule, note, error)` for editor feedback and paste import; `parse_permission_rule(text) -> PermissionRule` raises instead.
- `check_permission_rules(value) -> RuleSetCheck(rules, notes, problems)`: the same validation, reporting every problem as `RuleSetProblem(part, index, rule, message)` (unknown parts first) so an editor places each one; `rules` is set only without problems.
- `permission_rules_to_dict(rules)`: the normalized persisted form, only set parts; it round-trips through the normalizer. An empty `allow: []` stays set.
- `PermissionRules.restricts` (a narrowed base or a non-empty `deny`; `allow` only widens a narrowed base) is the one decision whether a set is full access.
- `resolve_rule_set(*levels) -> PermissionRules | None`: per part, the first level (most specific first) that sets it wins; `None` (full access) without any rule set or when the result restricts nothing. Pass all levels in one call: `None` hides the parts set.
- Bases: `read` `anywhere|work_area`, `write` `anywhere|work_area|scratch`, `commands` `any|listed`, `web` `any|listed`; defaults are the unrestricted values. Kinds `Read`, `Edit`, `Shell`, `Web`; aliases `Write`, `MultiEdit`, `NotebookEdit` -> `Edit`, `Glob`, `Grep`, `LS`, `List` -> `Read`, `Bash`, `PowerShell` -> `Shell`, `WebFetch(domain:x)` -> `Web(x)`. Placeholders `{workdir}`, `{project}`, `{scratch}`, `{workspace}`, `{home}` and `~` may only start a path pattern; a relative pattern resolves against `{workdir}`.
- Presets (`_presets.py`, the one definition for the CLI and, through `permissions.validate`, the WebUI): `full_access` (every base unrestricted, `allow`/`deny` `[]`), `work_dir` (`read`/`write` `work_area`), `work_dir_read_only` (`read` `work_area`, `write` `scratch`). `apply_preset(name, stored)` unsets `commands` and `web`, then sets every part the preset defines (so `full_access` replaces everything, the others keep `allow`/`deny`); `matching_preset(rules)`: `full_access` for `None` or a set that restricts nothing, else by the bases only, or `custom`; `preset_name(text)` accepts hyphens, underscores and spaces. A preset is a shortcut, not a stored form.
- `describe_permission_rules(rules, *, values)` / `describe_layer(layer)`: one layer as the user reads it (`source`, `bases`, `workdir`, readable and writable folders or `None` for no limit, allowed commands and hosts or `None`, deny rules, and for the Agent's own layer the `inactive` rules whose placeholder has no value). Unknown placeholders stay as written (`{scratch}`).

**Boundary:**
- `PermissionBoundary.build(rules, context, *, ceilings=()) -> PermissionBoundary`; `BoundaryContext(workdir, home, data_dir, project_root, scratch_root, workspace, agent_config, install_dirs, readable_dirs, loopback_port, platform)`. A rule whose placeholder has no value in this Run is dropped.
- `check_path(path, access, *, base=None, subtree=False)` with `access` `read`, `write` or `start_dir` (the directory a process starts in, decided like a read); `subtree` (a recursive delete, copy or listing) also refuses when a protected location lies below the path or a deny rule could match below it (segment by segment, `**` always can). `check_host(host)`; `check_url(url)` (http/https only; the vBot server's own port on a host `is_own_host` names - loopback and unspecified addresses, `localhost`, this machine's name, not its LAN addresses - is refused). Each returns `None` or a `PermissionRefusal`.
- `refusable_below(folder, access) -> tuple[str, ...]`: patterns, relative to a folder, of what below it may still be refused (deny rules, `agent.json`, install dirs for writes, the data directory; `**` is the whole folder). Below a permitted folder only these and links leading out can refuse, because bases and allow rules cover whole subtrees. `readable_entries(folder, entries)` keeps the `os.DirEntry`s of a listing that may be read and checks only links and junctions unless `refusable_below` names something. Both cost nothing under full access; listings, suggestions and search exclusions use them (`tools/file_state.md` -> Path resolution).
- For command checks: `restricted`, `layers` (`PermissionLayer.commands`, `shell_allow`, `shell_deny`), `context`, `loopback_port`, `refusal(...)` (builds a refusal with the allowed summary) and `allowed_summary(access)`.
- `to_ceiling_dict(*, source)` / `ceiling(*, source)` turn the boundary into a ceiling for a Run it causes (its own layer retagged as `source`, inherited layers keep theirs); `PermissionCeiling.from_ceiling_dict(value)` reads one strictly (exact fields, every base present, absolute placeholder-free path patterns).
- `normalize_path(path, *, base, home, platform)`: the lexical and resolved forms exactly as the checks compare them.

**Command checks** (`CommandChecker`, one per Runtime, `runtime._command_checker`): `check_line(boundary, command_line, *, shell, cwd, env, home)` for a shell Tool call, a terminal line or a Cron watch command; `check_argv(boundary, argv, *, cwd, env, home)` for a program a terminal starts. Each returns `None` or a `PermissionRefusal`. A full-access boundary returns at once; the first restricted check creates the `CommandAnalyzer` through the Runtime's factory (`core/runtime/_bootstrap.py::_command_analyzer`, the shell's own `pwsh`), so a server whose Agents all have full access never starts the PowerShell helper. `aclose()` (shutdown step `command_analysis`, `runtime/lifecycle-steps.md`) stops it; later checks refuse. Order: the start directory (`start_dir`), the analysis (`CommandAnalysisUnavailable` refuses, fail closed), every simple command against deny rules of every layer (`command_pattern_may_match`), every `commands: listed` layer's allow rules (`command_pattern_matches`), vBot's own CLI (`vbot` as a program or `.ps1`, a Python running `<vbot>/cli/...` or `server/main.py`, `python -m cli...`/`server...` in a folder inside the checkout), the vBot server (a `host:port` word, or an own-host word plus a word that is the port), every URL (`check_url`), every path access (`check_path` with its `subtree`), and last every unresolved element, which always refuses (`unanalyzable` with a fix in `hint`): the protected locations restrict every restricted Session, so no unresolved path or command is harmless. Tests: `tests/core/permissions/test_command_check.py` over `command_check_support.FakeAnalyzer`, which the Tools' tests use too.

**Users** (where the library is wired in):
- Stored rule sets: `agent.json` `permission_rules` (`agent/configuration.md`), `defaults.agent.permission_rules` (`settings.md`), the Project override `overrides.<agent>.permission_rules` (`projects/configuration.md`), `AgentProfile.permission_rules` of a repository profile (`projects/scanning.md` -> Permission Rules translation) and `TemporaryAgentConfig.permission_rules` / `.permission_ceiling` (`agent/temporary-agents.md`). Each owner normalizes on write; a stored set that does not normalize, an unknown part included, makes its document invalid and unwritable, never unrestricted.
- Resolution: `RuntimeAgent.permission_rules` (`None` or a set, which may restrict nothing) and `RuntimeAgent.permission_ceiling` (temporary Agents); provenance in `effective_config(...)["permission_rules"]` (`projects/resolution.md` -> Permission Rules).
- Run: `core/chat/_run_permissions.py::run_permission_boundary` builds the boundary once per Run; Tools read `ToolContext.permissions`. `ToolContext.resolve_path(..., access=)` checks `read`, `write` and `start_dir` and raises `PermissionDenied` (`tools/file_state.md` -> Path resolution).
- Paths beyond the Tool target: listings, missing-path suggestions, diagnostics, search roots and exclusions, image, media and Channel files, the `project` Tool's instruction files, MCP roots and Assistant `file:` references are checked before any access, so a refused path is neither touched nor named (`tools/file_state.md` -> Path resolution, `chat/request-building.md` for file references, `extensions/mcp.md` for roots).
- Shell: `bash`/`powershell` check every call before launch and run it in a confined environment (`tools/shell.md` -> Permission Rules).
- Cron watch checks of a job with a ceiling: the command and URL at every check and when the `cron` Tool saves the watch (`automation/cron-watch.md`).
- Web: `web_fetch` at the address, every redirect hop, a service's final URL and the browser's final page; `analyze_image` image URLs and their hops (`fetch_public(..., permissions=)`); `web_search` drops results it may not fetch, with a count (`tools/web_fetch.md`, `tools/web_search.md` -> Permission Rules).
- Terminal: `start` (program, folder), lines submitted at an integrated shell prompt, and `attach`/`status` only for terminals started under the same or narrower rules (`tools/terminal.md` -> Permission Rules).
- Ceilings: `AgentOverrides.permission_ceiling` of a Sub-Agent Session (`source` `parent`), `CronJob.permission_ceiling` (`source` `cron`, `CronJob.ceiling()`), `TemporaryAgentConfig.permission_ceiling` (`source` `extension`).
- Editors and summaries: RPC `agent.create`/`agent.update` `permission_rules`, `permissions.validate` and `permissions.session` (`server/session-agent-rpcs.md` -> Permission Rules), the CLI permission flags of `agent` and `project set-override` (`cli/agent-commands.md`), the WebUI editors, the Session `Restricted` badge and refused Tool rows (`webui/settings.md`, `webui/projects.md`, `webui/chat.md`). For a built boundary, `allowed_summary(access)` per access and `permissions_block_text(boundary)` for the text the Agent reads.

**Refusal:** `PermissionRefusal(access, target, reason, source, rule, base, location, detail, resolved, allowed, hint)`; `reason` is `deny_rule`, `protected_location` (`location` `data_dir`, `installation`, `agent_config`, `vbot_server`, `vbot_cli`), `not_permitted` (by `base` of layer `source`), `unsafe_form` (`location` names the form) or `unanalyzable` (`detail` is the caller's clause; `hint` the sentence saying how to write it so it can be checked, which replaces the closing "tell the user"; an `unanalyzable` refusal leaves out that every other way is refused too, since a rewritten command can pass). `refusal.message(tool=...)` is the Agent-facing text; `PermissionDenied(refusal)` raises it through a Tool.

## Decision order

For a path, in this order:
1. Normalize: `~`, relative paths joined to `base` (default: the working directory), separators, `.`/`..`, Windows trailing dots and spaces, `\\?\` drive prefixes, 8.3 short names (`GetLongPathNameW` on the longest existing prefix). Network paths (UNC, `\\?\UNC\`, `file://host`), device paths (`\\.\`, `\\?\GLOBALROOT`, NT `\??\`), alternate data streams and drive-relative paths are recognized lexically before any filesystem access and refused unless an allow rule without wildcards names the path or a folder above it.
2. Resolve links and junctions with `os.path.realpath` (non-strict); POSIX resolves the joined path as written, so `link/..` climbs out of the link target as the kernel does, Windows the lexical form, as Win32 does. Both forms must pass every later step; a refusal then names where the link leads (`resolved`).
3. Protected locations: the Agent's `agent.json` (always); install and interpreter dirs (writes); the data directory, except the Workspace, the Scratch Area and `readable_dirs` for reading, and the Scratch Area or a Workspace that is the working directory for writing.
4. A deny rule of any layer refuses: `Read` deny covers reads and writes, `Edit` deny writes.
5. Every layer must permit, by its base or an allow rule: an `Edit` allow covers reads too. The Workspace and `readable_dirs` count as readable in every layer.

Hosts and commands: a deny rule of any layer refuses; under `listed` every such layer needs an allow rule. Host patterns: `example.com` covers the host and its subdomains, `*.example.com` only subdomains, `*` every host, IP literals match exactly.

## Constraints & Gotchas

- `UNRESTRICTED` (every boundary without layers) answers each check before any work; keep that the first statement of every new check: full-access Agents pay nothing.
- A restricted check touches the filesystem (`realpath`, 8.3 expansion), and `build` resolves every folder and pattern prefix once. Unsafe forms are never resolved, so a UNC path never contacts SMB.
- A path pattern covers what it names and everything below it, with or without wildcards (`{workdir}/secret*` covers `{workdir}/secrets/key.pem`). Matching is case-insensitive on Windows (`casefold`), never on POSIX.
- Tool paths do not expand placeholders: `{workdir}` in a Tool argument is a file name, as in `ToolContext.resolve_path` (`tools/file_state.md` -> Path resolution); only `~` expands.
- The library protects only the folders the caller names: which folders count as installation or as readable vBot artifacts (and whether a development checkout is an installation) is the caller's decision.
- Ceilings are self-contained: they store their own Work Area folders, so a Sub-Agent working elsewhere stays inside its parent's area.

## Agent-facing text

This domain owns the refusal text (`PermissionRefusal.message`, `core/permissions/_refusal.py`), which Tools return as `permission_denied`. Before you change, add or delete a sentence of it, read the `permission_denied` rows in `tools/agent-facing-text.md`: they record why each sentence exists. Update the row for every sentence you change and add one for every sentence you add.

## Tests

`tests/core/permissions/`: rule language and resolution (`test_permission_rules.py`), boundary decisions, protected locations, ceilings, hosts and URLs (`test_permission_boundary.py`), path normalization, unsafe forms without filesystem access, links and junctions (`test_permission_paths.py`; Windows-only cases skip elsewhere). The failure code: `tests/core/tools/test_tools_execution.py::test_failure_code_follows_the_phase_that_raised`.

## References

Read these only when your task matches - not by default.

- Changing how shell command lines are analyzed (CommandPlan, PowerShell helper, path tiers, cwd tracking, Shell(...) patterns) -> `permissions/commands.md`
