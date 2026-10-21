# Permission Rules language

Read when changing how stored rule sets are parsed, validated, resolved, described or edited: the rule strings and their import spellings, placeholders, presets, the per-layer descriptions, and how editors, RPC, CLI and repository translation hand rule sets to the library. The boundary that decides accesses and the protected locations are in `../permissions.md`; path matching in `paths.md`.

## Stored form and validation

Stored form `{read, write, commands, web, allow, deny}`, every part optional:

- `normalize_permission_rules(value, *, notes=None, platform=sys.platform) -> PermissionRules`: the one strict validator for every stored form; raises `PermissionRulesError` (a `ValueError`) naming the part, the value and what is accepted. Unknown parts are refused. `None` parts count as not set. Rule strings rewritten from another harness's spelling (aliases, bare `Bash`, `:*`, `domain:`, a URL, Claude Code's `//path`) add a sentence to `notes`; separator and whitespace normalization is silent. A stored set with `write: anywhere` beside `read: work_area` is refused (`../permissions.md` -> Requirements, item 6).
- `normalize_permission_rule(text) -> RuleNormalization(rule, note, error)` for editor feedback and paste import; `parse_permission_rule(text) -> PermissionRule` raises instead.
- `check_permission_rules(value) -> RuleSetCheck(rules, notes, problems)`: the same validation, reporting every problem as `RuleSetProblem(part, index, rule, message, reason)` (unknown parts first) so an editor places each one; `message` names the field path for CLI and API callers (`permission_rules.allow[0] "Web(exa mple.com)": ...`), `reason` (`PermissionRulesError.reason`) the same without it, which editors show beside the part or rule (`"Web(exa mple.com)": ...`). `rules` is set only without problems.
- `permission_rules_to_dict(rules)`: the normalized persisted form, only set parts; it round-trips through the normalizer. An empty `allow: []` stays set.
- `PermissionRules.restricts` (a narrowed base or a non-empty `deny`; `allow` only widens a narrowed base) is the one decision whether a set is full access.

## Bases, kinds and placeholders

- Bases: `read` `anywhere|work_area`, `write` `anywhere|work_area|scratch`, `commands` `any|listed`, `web` `any|listed`; defaults are the unrestricted values.
- Kinds `Read`, `Edit`, `Shell`, `Web`; aliases `Write`, `MultiEdit`, `NotebookEdit` -> `Edit`, `Glob`, `Grep`, `LS`, `List` -> `Read`, `Bash`, `PowerShell` -> `Shell`, `WebFetch(domain:x)` -> `Web(x)`. `Shell(...)` patterns: `commands.md` -> Patterns.
- Placeholders `{workdir}`, `{project}`, `{scratch}`, `{workspace}`, `{home}` and `~` may only start a path pattern; a relative pattern resolves against `{workdir}`. A rule whose placeholder has no value in a Run is dropped from that Run (`PermissionBoundary.build`).
- Host patterns (`Web(...)`): `example.com` covers the host and its subdomains, `*.example.com` only subdomains, `*` every host, IP literals match exactly, an IPv4 address in its dotted form whichever form a rule or access writes (`127.1`, `_addresses.py`). The same rules decide hosts that ssh, scp and `git@host:` reach (`check_host(host, "connect")`).
- The vBot server's own port is refused on every host `is_server_host` names (`check_url`): loopback and unspecified addresses in any IPv4 form, `localhost`, this machine's name, and `own_hosts`, which the server fills while it listens beyond loopback - every interface address, its names and the access configuration's host names (`access.md` -> ServerHosts).

## Resolution

`resolve_rule_set(*levels) -> PermissionRules | None`: per part, the first level (most specific first) that sets it wins; `None` (full access) without any rule set or when the result restricts nothing. Pass all levels in one call: `None` hides the parts set. When the parts come from different levels, writing narrows to `work_area` under `read: work_area` (`write_within_read`). Which levels exist for each Agent kind: `projects/resolution.md` -> Permission Rules.

## Presets

`_presets.py` is the one definition for the CLI and, through `permissions.validate`, the WebUI: `full_access` (every base unrestricted, `allow`/`deny` `[]`), `work_dir` (`read`/`write` `work_area`), `work_dir_read_only` (`read` `work_area`, `write` `scratch`). `preset_definitions()` gives each as the change it makes (`commands` and `web` `None`, which removes them, then every part the preset defines), and `apply_preset(name, stored)` applies that change (so `full_access` replaces everything, the others keep `allow`/`deny`); `matching_preset(rules)`: `full_access` for `None` or a set that restricts nothing, else by the bases only, or `custom`; `preset_name(text)` accepts hyphens, underscores and spaces. A preset is a shortcut, not a stored form. Full access stores the explicit full set, so it overrides a restrictive default (`../permissions.md` -> Requirements, item 7).

## Descriptions

`describe_permission_rules(rules, *, values)` / `describe_layer(layer)`: one layer as the user reads it (`source`, `bases`, `workdir`, readable and writable folders or `None` for no limit, allowed commands and hosts or `None`, deny rules, and for the Agent's own layer the `inactive` rules whose placeholder has no value). Unknown placeholders stay as written (`{scratch}`). `permissions.session` shows these per layer for a Session; editors show the effective parts.

## Editors

- `editor_bases(own, inherited)` (`_presets.py`): per base part what applies (`value`, `source` `own`, `inherited` or `default`), what applies without an own value (`inherit`, write already narrowed by the read that applies), the `choices` and the `excluded` ones (write wider than that read), plus `write.narrowed_by_read`, the table an editor uses to narrow an own write when it changes `read`. It reads `own` in stored form, so a candidate with problems still gets its bases.
- RPC `permissions.validate` (`server/session-agent-rpcs.md`): checks a candidate rule set without saving and returns what an editor needs, so no client re-implements the rule language: the normalized rules, notes and problems, the matching preset, the presets as changes, and `editor_bases`.
- CLI: the permission flags of `agent create/update` and `project override set` (`cli/agent-commands.md`) apply presets with the core `apply_preset` and validate through the same normalizer.
- Repository translation: Claude Code, OpenCode and Codex permission settings become rule sets with notes (`projects/scanning.md` -> Permission Rules translation).

## Tests

`tests/core/permissions/test_permission_rules.py` (parsing, aliases, notes, validation, resolution, presets, descriptions); what editors receive, `editor_bases` included: `tests/server/rpc/test_permission_methods.py`.
