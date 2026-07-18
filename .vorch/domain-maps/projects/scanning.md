# Project Scanning

Read this reference when changing repository Agent discovery, supported source formats, format detection, scan findings, or collision behavior.

## Scanner Boundary

`core/projects/scanners/base.py` defines the `AgentDetector` contract and `ScannedAgent` representation. The scanner registry has one detector per supported source format and a stable rank: OpenCode first, Claude second. A persisted Project selects exactly one detector through `source_format`; multi-format detection is advisory and does not merge formats into one Team.

A `ScannedAgent` carries the normalized Agent id and display metadata plus the repository-derived runtime inputs needed by resolution: description, raw model, temperature, instructions body, source format/path, denied Tools, ordered Agent-target rules, and thinking effort. `AgentTargetRule` is the source-neutral pattern/allow representation; the resolver materializes it only against the current Project Team. A scanned Agent does not contain Project defaults, per-Agent overrides, global defaults, or the final effective capability set.

## Format Mappings

### OpenCode

`core/projects/scanners/opencode.py` reads Agent files directly under `.opencode/agents/`; discovery there is non-recursive. It parses frontmatter and body, retains supported model/temperature/thinking fields, converts explicit Tool or permission denials into `denied_tools`, and maps ordered scoped Sub-Agent target rules into `agent_target_rules`. Unknown or malformed permission structures fail open rather than accidentally disabling capabilities.

### Claude

`core/projects/scanners/claude.py` recursively reads `.claude/agents/**/*.md`. It maps Claude Tool allow/deny metadata into the common denial representation, maps scoped `Agent(...)`/`Task(...)` entries into ordered `agent_target_rules`, and intentionally drops Claude's model field instead of treating it as a vBot model id. A scoped target denial narrows the Project Team but does not by itself disable the entire Sub-Agent capability.

Keep source-specific parsing inside the detector. Downstream Team and resolver code should consume the common `ScannedAgent` shape and must not branch on repository file syntax.

## Format Detection

`detect_project_formats()` is a read-only, fail-soft inspection used before or around Project creation. It reports per-format Agent counts and loadable Project Skill counts, plus relevant `AGENTS.md`/`CLAUDE.md` instruction-file facts. Detection may surface more than one format; selection of the persisted `source_format` remains a separate Project configuration decision.

Failures to inspect one candidate should become detection/report information where possible rather than preventing every other format from being considered.

## Team Assembly & Findings

Team construction and scan reporting are deterministic. Candidates are ordered by detector rank and then filename; the resulting Team is sorted by `agent_id`. Slug normalization determines the runtime Agent id.

`core/projects/scan_report.py` owns scan findings:

- `BAD_MODEL`: a repository model value cannot be used as configured.
- `SLUG_COLLISION`: multiple source files normalize to the same Agent id.
- `UNSLUGIFIABLE_NAME`: a source name cannot produce a valid Agent id.
- `ORPHAN`: persisted per-Agent state references an Agent no longer present in the selected repository source.
- `UNAVAILABLE_TOOL`: a persisted Project Tool Whitelist entry is not currently a registered Project tool. `server/rpc/project_methods.py` appends this runtime-owned finding through `ScanReport.with_tool_findings`; the repository scanner does not consult the Tool Registry, and the permission remains stored so a disabled Extension can regain it.

Do not silently resolve collisions according to filesystem enumeration order. Preserve stable detector/file ordering and expose findings so callers can explain why a candidate was excluded or degraded.

## Cache Interaction

The resolver may cache Team membership per Project, not repository Agent configuration. `project.show` deliberately reloads Skills, invalidates relevant caches, and rescans. Changes to Project `cwd` or `source_format` must invalidate membership because they change the discovery source.

When adding a source format, implement the detector, register its stable rank, add format detection and Project Skill discovery behavior, extend the accepted `source_format` contract and WebUI choice, and cover cross-format ordering/collision behavior. Do not make the resolver understand the new file syntax.

## Source & Tests

- Shared detector and scanned shape: `core/projects/scanners/base.py`
- Registry and scan orchestration: `core/projects/scanners/base.py`
- OpenCode mapping: `core/projects/scanners/opencode.py`
- Claude mapping: `core/projects/scanners/claude.py`
- Findings: `core/projects/scan_report.py`
- Primary tests: `tests/core/projects/scanners/`, `tests/core/projects/test_scan_report.py`, and `tests/core/projects/test_resolver_scan_identity.py`
