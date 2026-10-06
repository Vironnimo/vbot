# Project Sources

Read for adapters, discovery, translation, source priority or findings.

## Ownership and inputs

`core/projects/sources/` is an internal part of Projects. `profile.py` defines `AgentProfile`, ordered Tool/target rules, translations and Source selections. `catalog.py` owns detection and collision-safe refresh. `sources.py` assembles the Team and shadowed definitions and reads live Profiles. Downstream code never interprets foreign syntax.

`opencode.py` reads Markdown in `.opencode/agents/` and `.opencode/agent/`, plus Agent objects and global rules in `opencode.json`, `.opencode/opencode.json` and their `.jsonc` variants (comments and trailing commas allowed). Entries that only adjust OpenCode's built-in Agents (`build`, `plan`, `general`, `explore` without a `prompt`) define no Team member. Prompt file references stay inside the repository. `markdown.py` reads Claude (`.claude/agents/` and repository settings), Copilot (`.github/agents/`), Cursor (`.cursor/agents/`) and Gemini (`.gemini/agents/`). Markdown without frontmatter is documentation, not an Agent (Copilot `*.agent.md` files excepted). `codex.py` reads `.codex/agents/*.toml` and role config-file references from `.codex/config.toml`, whose sandbox, approval, permission-profile, web search and feature settings every Codex Agent inherits. Discovery is recursive within known folders and never traverses links/junctions. `_reading.py` limits inputs to 128 KiB, accepts UTF-8, rejects duplicate metadata keys and normalizes line endings while retaining instructions.

Skill Sources include `.agents/skills/` and each supported ecosystem's Skill directory. Skills loads these ordered roots, first name wins. Optional instruction Sources include CLAUDE.md, GEMINI.md and Copilot instructions. AGENTS.md remains the creation-seeded auto-load default. Instruction files usually repeat each other, so a detected instruction Source starts active only while no instruction file loads yet (an existing auto-load file or an active instruction Source). Cursor conditional/path rules are excluded, never flattened into always-on instructions.

## Translation and access

vBot grants whole Tools and has no sandbox, approvals or per-command and per-path rules (user decision 2026-10-06). An Agent that may do something in its own tool, even only for some commands or paths, after approval or inside a sandbox, gets the vBot Tool that does it; only a capability the Agent lacks entirely removes the Tool: an unscoped denial, an explicit Tool list without it, a read-only mode, a disabled feature. Sandboxes, approvals, hooks and isolation therefore change no Tool; an Agent's own hooks and other behavior vBot does not run are reported. Broken or unreadable metadata still makes the Profile unavailable with a finding.

Every behavior/access setting is applied, translated, not supported or overridden by vBot. A foreign value vBot lacks (an effort level, say) is reported and dropped; only malformed metadata makes a Profile unavailable. A Model wish list uses its first entry. Claude, Cursor, Gemini and OpenCode subagents without a Model wish inherit the delegating caller's Model. Appearance settings can be dropped; other unknown settings are reported. Status is ready, limited (unsupported settings) or needs_attention (unavailable). Disabled OpenCode Agents remain visible and unavailable.

Claude retains Model wishes, effort and Skill preloads. Explicit Tool lists become exact selections; empty means no Tools; `Read(src/**)` grants `read`. A Bash denial covers bash and terminal. `disallowedTools` and `permissions.deny` remove a Tool only when unscoped; `ask` rules change nothing. Scoped Agent/Skill names are represented as target rules. Permission modes: `plan` keeps read and search only, `dontAsk` keeps only Tools its `permissions.allow` rules pre-approve, and the other modes leave the Tools unchanged. MCP, isolation, memory, background, turn limits and hooks remain in the report; no external engine, hook or sandbox is run.

OpenCode global rules precede Agent rules with ordered last-match precedence. A capability is usable when any of its rules allows or asks; a wildcard deny with later allows keeps only those. Several capabilities can share a vBot Tool (Grep and Glob for search); any usable one grants it. A denied capability vBot does not have (`todowrite`, `lsp`) changes nothing. Codex `read-only` sandboxes or permission profiles remove patches; other sandboxes and approval policies change nothing; `web_search = "disabled"` and `features.shell_tool = false` remove their Tools. Copilot `agents` lists limit delegation targets; qualified Tool names (`read/readFile`) map through their Tool set. Cursor readonly selects read capabilities. Gemini local Profiles prohibit recursive delegation; remote Profiles are unavailable.

## Priority, refresh and findings

Source order decides winners, then stable path order within a Source. The Team is sorted by Agent id. Losing definitions remain in `ScanResult.shadowed`; duplicate Skill names produce findings. Off Sources keep their rows. New Sources append active unless they collide with an existing active name. Existing Projects use the named additive backfill (`configuration.md`). Optional `agent_paths` limits a Source to repository-relative file patterns; the backfill retains old top-level Markdown scope. Excluded definitions stay visible with `source_scope` findings. Explicit expansion removes that limit; adapters and runtime configuration stay format-neutral.

`ScanReport` owns records only: bad_model, slug_collision, unslugifiable_name, orphan, unavailable_tool, invalid_source, skill_collision and source_scope. Source assembly supplies structural findings; resolution supplies usable-Model and pointer findings; RPC supplies unavailable Project Tools. Each Profile also carries its translation report.

Membership can be cached; each Run rereads the selected Profile. Project show/open rescans and refreshes detection. Skill registry/inventory rebuilds also refresh Sources. Mutations of order, activation, cwd or Model mappings invalidate Team/Skill projections.

## Source and evidence

- `core/projects/sources/`, `source_migration.py`, `scan_report.py`
- `tests/core/projects/test_sources.py`: mixed sources, collisions, all adapters, empty/scoped permissions, legacy anchors, Models, preloads and participant snapshots
- Retained resolver/Store, Skills, RPC and Extension-host tests cover their public contracts.
- Schema references: https://opencode.ai/docs/agents/, https://code.claude.com/docs/en/sub-agents, https://developers.openai.com/codex/multi-agent, https://docs.github.com/en/copilot/reference/custom-agents-configuration, https://cursor.com/docs/subagents, https://geminicli.com/docs/core/subagents/
