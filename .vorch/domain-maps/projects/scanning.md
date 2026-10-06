# Project Sources

Read for adapters, discovery, translation, source priority or findings.

## Ownership and inputs

`core/projects/sources/` is an internal part of Projects. `profile.py` defines `AgentProfile`, ordered Tool/target rules, translations and Source selections. `catalog.py` owns detection and collision-safe refresh. `sources.py` assembles the Team and shadowed definitions and reads live Profiles. Downstream code never interprets foreign syntax.

`opencode.py` reads Markdown in `.opencode/agents/` and `.opencode/agent/`, plus Agent objects and global rules in `opencode.json` and `.opencode/opencode.json`. Prompt file references stay inside the repository. `markdown.py` reads Claude (`.claude/agents/` and repository settings), Copilot (`.github/agents/`), Cursor (`.cursor/agents/`) and Gemini (`.gemini/agents/`). `codex.py` reads `.codex/agents/*.toml` and role config-file references from `.codex/config.toml`. Discovery is recursive within known folders and never traverses links/junctions. `_reading.py` limits inputs to 128 KiB, accepts UTF-8, rejects duplicate metadata keys and normalizes line endings while retaining instructions.

Skill Sources include `.agents/skills/` and each supported ecosystem's Skill directory. Skills loads these ordered roots, first name wins. Optional instruction Sources include CLAUDE.md, GEMINI.md and Copilot instructions. AGENTS.md remains the creation-seeded auto-load default. Instruction files usually repeat each other, so a detected instruction Source starts active only while no instruction file loads yet (an existing auto-load file or an active instruction Source). Cursor conditional/path rules are excluded, never flattened into always-on instructions.

## Translation and access

Every behavior/access setting is applied, translated, not supported or overridden by vBot. Appearance settings can be dropped. Unknown behavior settings are reported; unknown permission/isolation/hook/Tool settings disable Tools conservatively. Status is ready, limited (unsupported settings) or needs_attention (unavailable). Broken/unreadable metadata creates an unavailable Profile with a finding, never default access. Disabled OpenCode Agents remain visible and unavailable.

Claude retains Model wishes, effort and Skill preloads. Explicit Tool lists become exact selections; empty means no Tools. A combined Tool requires all capabilities it supplies (Edit+Write for patches; Grep+Glob for search). Shell restrictions cover bash and terminal. Explicit `ask` rules and scoped command/path requirements disable affected Tools; scoped Agent/Skill names can be represented. Permission modes describe how Claude Code consults its user, and vBot asks no approvals: `plan` keeps read and search only, `dontAsk` keeps only Tools its `permissions.allow` rules pre-approve, and the other modes leave the vBot Tool access unchanged. `PreToolUse` and `PermissionRequest` hooks can block Tool calls, so the Tools their matchers name are disabled; observing hooks in repository settings (session start, after a Tool call) belong to Claude Code's session and are ignored, while an Agent's own hooks are reported. MCP, isolation, memory, background, turn limits and other unsupported behavior remain in the report; no external engine, hook or sandbox is run.

OpenCode global rules precede Agent rules with ordered last-match precedence. Wildcard deny cannot be lost. Capability overlap is evaluated before output Tools, so Glob allow cannot undo Grep deny. Codex isolation and approval requirements fail closed. Cursor readonly selects read capabilities. Gemini local Profiles prohibit recursive delegation; remote Profiles are unavailable.

## Priority, refresh and findings

Source order decides winners, then stable path order within a Source. The Team is sorted by Agent id. Losing definitions remain in `ScanResult.shadowed`; duplicate Skill names produce findings. Off Sources keep their rows. New Sources append active unless they collide with an existing active name. Existing Projects use the named additive backfill (`configuration.md`). Optional `agent_paths` limits a Source to repository-relative file patterns; the backfill retains old top-level Markdown scope. Excluded definitions stay visible with `source_scope` findings. Explicit expansion removes that limit; adapters and runtime configuration stay format-neutral.

`ScanReport` owns records only: bad_model, slug_collision, unslugifiable_name, orphan, unavailable_tool, invalid_source, skill_collision and source_scope. Source assembly supplies structural findings; resolution supplies usable-Model and pointer findings; RPC supplies unavailable Project Tools. Each Profile also carries its translation report.

Membership can be cached; each Run rereads the selected Profile. Project show/open rescans and refreshes detection. Skill registry/inventory rebuilds also refresh Sources. Mutations of order, activation, cwd or Model mappings invalidate Team/Skill projections.

## Source and evidence

- `core/projects/sources/`, `source_migration.py`, `scan_report.py`
- `tests/core/projects/test_sources.py`: mixed sources, collisions, all adapters, empty/scoped permissions, legacy anchors, Models, preloads and participant snapshots
- Retained resolver/Store, Skills, RPC and Extension-host tests cover their public contracts.
- Schema references: https://opencode.ai/docs/agents/, https://code.claude.com/docs/en/sub-agents, https://developers.openai.com/codex/multi-agent, https://docs.github.com/en/copilot/reference/custom-agents-configuration, https://cursor.com/docs/subagents, https://geminicli.com/docs/core/subagents/
