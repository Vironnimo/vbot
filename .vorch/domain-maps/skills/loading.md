# Skill Loading

How Skill packages are scanned, parsed and checked: `SkillRegistry.load` and its scan roots, scoped registry caches and scan publication, the catalog text, SKILL.md frontmatter fallbacks, the vBot requirements metadata and diagnostics logging. Precedence, availability states and allowlist rules: `skills.md`.

## Scans and registry caches

`runtime.py` retains scoped registries, cache publication and sharing. `_global_scanning.py` owns global root ordering, policy exclusions and scans; `_inventory.py` owns manager inventory, package selection/disable and access projections, using the same runtime registry builders.

Non-obvious `SkillRegistry.load` behaviors: a scan root whose own path contains `SKILL.md` is a **package root** contributing exactly that package (how shared layers insert without scanning an owner's home); missing roots contribute nothing and one root's enumeration failure degrades to diagnostics without blocking startup; `environment` snapshots requirement-check inputs (defaults `os.environ`); `excluded_packages` (resolved Skill home, name) pairs skip exactly those packages before they claim their name, so a later root's same-named package loads, and keep them readable via `excluded_skills()` - runtime passes the Skill Policy's turned-off packages, editor loads omit them; `allowlist_exclusions` (Agent Skill Exclusions) instead keeps packages loaded and only narrows the allowlist grant.

An Identity Agent with exclusions always resolves such a scoped registry; the per-(Project, Agent) cache entry is reused only while it carries the Agent's current `excluded_skills`, so `agent.update` or a hand edit of `agent.json` needs no explicit invalidation (`test_runtime_skills.py`).

Scoped Project/Agent scans publish only if no invalidation or credential refresh occurred during I/O; cache access and publication are synchronized without holding the lock during scans (`test_runtime_skill_cache_races.py`). Global and scoped scans share the same global root ordering and origin rules.

`SkillRegistry.always_allowed` is scoped metadata: Identity registries contain enabled private names minus the Agent's `excluded_skills`, plus the active Project's effective names. A disabled private package grants nothing to a same-named global, bundled or shared replacement; that replacement needs its ordinary allowlist grant. The private name set uses the same `SkillScan` and package exclusions as the resolved registry. `SkillRegistry.allowlist_exclusions` holds the Identity Agent's `excluded_skills`; `filter_allowed`, `is_allowed` and the dependency checks of `availability_for` subtract it from the allowlist grant before adding `always_allowed` back (`test_skills.py`).

Shared layers are package roots inserted between the Project and global layers in sorted order (owner id, then Skill name), so first-found among same-named shared Skills is deterministic.

`SkillScan` shares source discovery and parsed metadata within one operation, never across refreshes; scoped registry builds reuse its reads for their name sets and shared packages (the inventory's use: `skills/manager.md`). Below it, `skills.py::_PARSED_SKILL_FILES` keeps parsed `SKILL.md` files for the process, the one Skill cache that outlives a refresh: keyed by input path, reused only while the resolved path, device/inode, size and modification time are unchanged, holding only loadable packages whose modification time is at least 2 s old (a write within the same timestamp tick could otherwise hide), bounded, and handing every reader copies of the mutable fields. It memoizes parsing only, never directory listings, so added and removed packages show at the next scan (`test_unchanged_skill_files_are_parsed_once_per_process`). Resource discovery (`core/skills/skills.py::scan_skill_resources`) excludes generated/VCS directories, links (Windows junctions included) and internal install receipts.

`core.skills.resolve_package_resource(root, resource)` resolves a portable package-relative resource, refusing generated/internal paths and resolved escapes before the reader checks existence or content. It preserves confined redirects; read/paging errors remain with the Tool.

## Catalog rendering

`core/skills/skills.py::format_skill_catalog_entries` renders prompt-facing metadata as origin headings with one `- name: description` line per Skill inside an `<available_skills>` boundary (`core/prompts/_formatting.py` wraps it for the `core:skills` block, `prompts.md`) - the same text as the `skill` Tool's no-argument list and the shape of the new-Skill System Reminder:

```text
<available_skills>
Bundled skills:
- weather: Get weather forecasts.
</available_skills>
```

- Skills group by **origin** (order: Bundled / Your global skills / Skills from project '<name>' / Your own skills; origin tags land at load from each scan root, project tags carry the display name). Originless registries render one "Skills" group.
- Each line holds only name and description (a multi-line description folds onto its line) - the catalog stays **path-free** as a prompt-economy presentation preference, not a hard routing rule. The explicit `project` Tool returns the loaded Project's Skills in its ordinary Result; afterwards the `skill` Tool resolves against that Project-aware registry while the pinned epoch catalog stays unchanged (see `tools/project.md`).
- Values are not escaped. The `core:skills` block follows `skill` Tool availability, also with an empty catalog: Skills can arrive mid-Session, so catalog contents do not gate it (`tests/core/prompts/test_prompts_tools_skills_extensions.py`). How to run scripts and read other package files is taught by each loaded Skill's `resource_files.guidance`, not by the block.
- The catalog includes only available+allowed Skills filtered against the same working-Project/private registry as live activation; `chat.commands` and `prompt.preview` resolve the Working Project of the named Session (`session_id`) or, without one, of a new Session (`working_project_id`; omitted: the Agent's default Project) (qualified Config Agents get no private layer), failing closed while preserving the saved reference; `chat.commands` offers no Skills for a Session whose Project no longer exists. `chat.commands`, requested whenever a Chat changes Agent address or the command catalog changes, resolves the Agent through `resolve_agent_async` and filters Skills on the catalog pool, never on the Event Loop (availability probes `PATH` for binary requirements). Its optional `session_id` (only with `agent_id`) resolves the Agent as that Session runs it, so a Librarian Session suggests the Skills of the Agent it maintains (`test_catalog_methods.py`).

## SKILL.md metadata

- Frontmatter prefers complete YAML (PyYAML) and degrades in staged fallbacks - BOM strip, re-quoting colon-space scalars, then top-level `key: value` extraction; without usable frontmatter the entire body is the Skill body. Missing name fills from the directory, missing description from the first body text line. Every fallback is a loadable warning, never a gate; name mismatches, overlong names, and trigger-unsafe characters also warn only - such a Skill loads but cannot be triggered via `/`/`$` (only explicit `skill` Tool argument). Two size warnings are likewise non-blocking and never cut anything (`skill_validator.py`, `test_skills.py::test_imperfect_metadata_loads_with_fallbacks_and_warnings`): a description over `MAX_SKILL_DESCRIPTION_LENGTH` (1024 characters, the Agent Skills specification limit; every catalog carries it in full), and stripped instructions over one page of `core/utils/text_pages.py` (50 KB or 2000 lines), which the `skill` Tool then returns in pages (`tools/skill.md`). The warnings reach the manager diagnostics and the `skill_manage` Result.
- YAML is read by `skill_validator.py::_load_yaml`: libyaml's C scanner and parser with PyYAML's Python composer and constructor (equal values, `RecursionError` at the same depth). Front matter libyaml reads differently - tabs, a byte order mark after the start, tags, a comment right after a block scalar header, `?` in flow collections - and every document it fails on go to the pure-Python `yaml.safe_load`, which alone decides errors and therefore fallbacks and warnings (`tab-after-value` and `comment-after-block-scalar-header` cases of the test above). Without libyaml the pure loader reads everything.

## vBot requirements metadata

vBot requirements live under `metadata.vbot.requirements` (never `compatibility`):

```yaml
metadata:
  vbot:
    requirements:
      all:
        - binary: git
        - any:
            - binary: gcc
            - binary: clang
        - any:
            - env: OPENAI_API_KEY
            - env: ANTHROPIC_API_KEY
      optional:
        - binary: jq
```

- `all` requires every child; `any` at least one; `optional` failures report without changing `available`.
- `env` checks a non-empty value in the snapshot environment (process env wins over the data-directory credential in the Secret Store). Names match case-insensitively on Windows (`os.name == "nt"`, where `os.environ` reports upper-cased names) and case-sensitively on POSIX (`core/skills/skills.py::_environment_key`, `test_skills.py`). Every declared `env` leaf doubles as an activation-time Bash grant surfaced via `environment_access` (Tool activations as sibling field, internal contexts wrapped before instructions), listing names and per-call `bash.env_keys` - permanent Agent grants are the separate `tools.bash.allowed_env` source (see `tools/shell.md`).
- `binary` resolves via `shutil.which` against the snapshot PATH - lookup, never shell execution. `skill` checks dependency loadable+available+allowed, walking chains with a cycle guard (circular -> `unavailable` with a cycle reason). Malformed requirements make the Skill invalid/non-loadable.

## Diagnostics

- Retain non-loadable directories as diagnostics (I/O failures, malformed requirements, duplicate names) so UI can explain them; YAML errors (including nesting beyond the parser's recursion limit and unconstructible scalar values such as invalid dates) and missing ordinary metadata use loadable fallbacks instead. The simple fallback ignores indented keys and block text, so nested metadata cannot replace top-level name or description. Unknown requirement keys, including YAML non-string keys, invalidate only their package rather than aborting the registry scan. Coverage: `test_imperfect_metadata_loads_with_fallbacks_and_warnings`.
- Metadata diagnostics log at `DEBUG` (`vbot.skills`) **once per process**, keyed by (path, warning) in a bounded `LoggedConditions` memory (a pair it forgot logs again), because registries reload on every project run/reload - only the log deduplicates; caller-facing diagnostics always carry every warning. For the same reason an unreadable or invalid policy file warns once per file version (modification time and size) and logs INFO once it is usable again (`SkillPolicyService`), and a stale shared entry warns once per owner or owner/Skill until it resolves (`SkillRuntime`), not once per receiver build.
