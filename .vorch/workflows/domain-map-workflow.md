# Domain Map Workflow

How to change domain maps and their supplementary files under `.vorch/domain-maps/`. Every map change follows the sections up to `## Creating or restructuring a map`; read the rest when you create, split, merge, rename or restructure a map. A corpus-wide cleanup follows `domain-map-audit-workflow.md`.

## What a root map is for

A root map (`.vorch/domain-maps/<domain>.md`) is read in full by every agent before it works in that domain, so each sentence costs every task there. It holds what an agent needs to safely touch anything in the domain (What belongs where). Supplementary files (`.vorch/domain-maps/<domain>/<topic>.md`) hold task-gated depth and are read only when a trigger in the root map's `## References` matches the task.

Maps are factual working notes for agents, not architecture documentation or generated reference: useful when they are correct and keep agents from touching the wrong layer. Short but wrong is worse than long.

## Size budget

A root map holds at most 16,000 characters (about 4,000 tokens); `.vorch/PROJECT.md`, which every agent reads in every session, at most 28,000. `python scripts/domain_maps.py` measures them; the commit hook warns when a staged file above its budget grows.

- When your change would leave a file above budget and larger than before, move material out in the same change so the file ends no larger than it started, beginning with the section you touched: from a root map, task-gated material into a supplementary file of that domain; from `.vorch/PROJECT.md`, facts whose use is confined to one domain into that domain's map.
- Content that every agent of the domain (for PROJECT.md: every agent) needs stays, even above budget. The budget tells you to look for task-gated material; it is never a reason to split always-needed content, which only forces an extra read.

## Every map change

1. **Place it.** Ask: would an agent working on a different feature of this domain still need this to choose the correct owner or layer, or to avoid breaking a domain-wide invariant? Yes -> root map. No -> supplementary file. Importance does not decide placement: a critical recovery contract of one feature still belongs in a supplementary file.
2. **Write it once.** Each fact has one canonical home; elsewhere use a pointer (`see sessions.md -> Interfaces`). When a supplementary file is created or its scope changes, add or sharpen its trigger in the root map's References.
3. **Write only what you verified** from source, tests, an existing map or `.vorch/PROJECT.md`, or an explicit user decision, never from memory. Keep observations, requirements and proposals distinguishable (`AGENTS.md` -> Interpret documentation). Remove unsupported claims or mark them as unresolved; never turn an unverified claim into a rule to keep it.
4. **Make the narrow update the work needs:** what changed in ownership, contracts, invariants, gotchas or documented behavior. A record of how the change was made belongs in the commit, not the map.
5. **Check the size budget** (above).
6. **Account for removals.** Before committing a change that moves or deletes content, compare old and new in the diff: every moved fact is present in its new home (read it there), and every deleted fact was stale, unsupported, duplicated elsewhere (read it at the owner), or mirrored source without decision value. When you move, rename or remove a heading or file, repoint every pointer to it: search for `-> <Heading>` and the file name in `.vorch/`, `AGENTS.md`, `.agents/skills/` and `scripts/README-*.md`.

For doc-only map work, run no application tests; verify by reading the source, adjacent maps and the diff.

## What belongs where

**Root map content** - eligible kinds, not a list of everything to keep there; exact payloads, state transitions and procedures of one feature belong in a supplementary file:
- Responsibility and boundary: what the domain owns and what it does not.
- Cross-domain touchpoints: which domains depend on it or feed it.
- Cross-task contracts other code relies on: public functions, events, API shapes, storage formats, message payloads, return shapes.
- Invariants ("only one active Run per Session") and decision rules ("fix this here, not there").
- Domain conventions beyond `AGENTS.md`.
- Constraints and gotchas: non-obvious behavior, fragile areas, earlier failures, security and performance traps.
- Source-of-truth pointers that let agents verify or extend behavior quickly.

**Supplementary file content:** feature-specific behavior and contracts, exact payload or state-transition detail, recovery matrices, step-by-step procedures ("how to add a Provider"), deep references (endpoint catalogs, probing recipes), and the reasons for Agent-facing text.

**Cut:**
- Inventories of RPCs, methods, fields, settings keys, CLI commands or schemas that restate what callers read in the code or the artifact itself; field-by-field code mirroring; per-component UI inventories.
- Rules already stated in `AGENTS.md` or `.vorch/PROJECT.md`.
- Architecture prose that does not change how an agent works in the domain.

Semantics a caller must know before or without running an artifact (flag consequences, exit codes, security rules, offline behavior) are not mirroring: keep them in the map or a supplementary file, never delegate them to `--help`, probing or source diving, because the task may be repairing exactly that artifact.

**Cross-cutting mechanisms.** A contract that any domain may need to use while it is defined in one place (example: kernel-to-Model notifications) gets its own cross-cutting home, a one-line pointer in `.vorch/PROJECT.md` -> Context, and a one-line back-reference in the References of each domain that uses it. Otherwise agents outside the owning domain never learn it exists.

## Agent-facing text

The reasons for runtime Agent-facing text (Tool descriptions and results, System Prompt blocks, System Reminders, prompts for Model jobs, bundled Skill instructions) live in the owning domain's supplementary file `<domain>/agent-facing-text.md`: a `| Text | Reason |` table with one row per sentence, giving the sentence or its unmistakable start and the failure it prevents, with its evidence where known (failure modes F1-F6: `tools.md` -> Terms).

- Read a sentence's reason before you rewrite or delete it: many record a failure seen in real Sessions that reading alone does not show. Change, add or delete its row together with the sentence.
- The root map keeps a short `## Agent-facing text` section that names the texts the domain owns and points to the file, so an agent changing them finds the reasons without loading a Skill:

```markdown
## Agent-facing text

This domain owns <the texts, by name and source path>. Before you change, add or delete a sentence of them, read `<domain>/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.
```

## Supplementary files and References

Supplementary files live in a folder named like their root map (`providers.md` -> `providers/add-a-provider.md`); the root map stays the entry point. Its References list each file with a trigger that tells an agent when to read it without opening it:

```markdown
## References

Read these only when your task matches - not by default.

- Adding or changing a Provider -> `providers/add-a-provider.md`
- Probing endpoints or verifying a Provider's API -> `providers/endpoint-probing.md`
```

- A vague trigger makes agents miss context; a sloppy split is worse than none.
- Write each entry's path relative to `.vorch/domain-maps/` or to the repository root; the commit hook blocks an entry that names no file.
- A supplementary file can route to deeper files through its own References; every supplementary file is reachable from its root map.
- Supplementary files never enter the Domain Maps index in `.vorch/PROJECT.md`, which lists root maps only. A child area that earns its own index row is a root map.

## Terms

A root map's `## Terms` section defines vocabulary needed only within that domain; shared vocabulary lives in `.vorch/GLOSSARY.md` (`AGENTS.md` -> Terminology). Omit the section when the domain has no terms of its own.

- One `### <Term>` per entry with a definition of one or two sentences, plus a distinction from nearby terms only where confusion is likely.
- Place it after the Overview, where useful opened by one line naming the relevant shared terms ("Core terms (Provider, Model) live in `.vorch/GLOSSARY.md`").
- Refer to a term in another map plainly ("see `models.md`") and to a shared term as "GLOSSARY -> <Term>". When a term moves, remove its old definition and repair the pointers to it.

## Formatting

Maps use ASCII punctuation: `->` for pointers, `-` for dashes, `...` for ellipses, `>=`, `<=`, `!=` and `~` for comparisons, straight quotes, because agents and tools match, diff and edit these files as plain text and look-alike Unicode characters break exact matches. A literal product string with other characters (a UI label, a rendered fragment) goes into a code span verbatim. The commit hook blocks non-ASCII characters outside code spans.

## Creating or restructuring a map

**Creating a root map**, when a new domain emerges or a domain has no map:
1. Identify the boundary: a module or subsystem with a clear responsibility where working without context risks misunderstanding interfaces, ownership or conventions. Use a nested root map only when a child domain has enough independent contracts or gotchas to warrant its own index row.
2. Gather evidence from `.vorch/PROJECT.md`, related maps, user decisions, source and tests.
3. Write only the template sections that apply, within the size budget, with task-gated material in supplementary files from the start.
4. Add the map to the Domain Maps index in `.vorch/PROJECT.md`.

**Restructuring a map** (splitting, merging, renaming, or cleaning up one map):
1. Decide per fact, by what it helps agents do: root map, supplementary file, pointer to another canonical home, or deletion.
2. Verify the facts you keep against source and tests.
3. For a substantial or judgment-heavy rewrite, briefly tell the user what stays, moves or goes before you write. Routine factual maintenance needs no planning round; ask only when an open choice would change meaning or scope.
4. Update the Domain Maps index when a root map was created, renamed, split or removed.
5. Account for every removed fact and repoint every pointer (Every map change, step 6), then run `python scripts/domain_maps.py` for routing defects.

## Template

Remove every section that does not apply; what each may hold: What belongs where.

```markdown
# <Domain Name>

<One sentence: what this domain does and where it sits in the system.>

## Overview

[What the domain owns, and what it does not do where that is not obvious.]

## Terms

[Domain-local vocabulary, one `### Term` each.]

## Data Model

[Only if the domain owns entities or persisted data: key shapes, relationships and invariants needed across the domain.]

## Interfaces

[Cross-task contracts other parts of the system depend on: exported functions and classes, API boundaries, event families, message or storage formats, return shapes.]

## Conventions

[Patterns specific to this domain beyond global rules: error handling, naming, async behavior, extension and testing patterns, ownership boundaries.]

## External Dependencies

[Only if the domain owns or calls external services, APIs, SDKs or infrastructure: auth, limits, quirks and failure behavior relevant for development.]

## Constraints & Gotchas

[Non-obvious behavior, fragile areas, earlier bugs, security and performance traps, things that look safe to change but are not.]

## Agent-facing text

[Only if the domain owns runtime Agent-facing text: the pointer section from Agent-facing text above.]

## References

[Only if the domain has supplementary files: one trigger line per file.]
- <When an agent reads it> -> `<domain>/<file>.md`
```

## Output

When the map work is done, report which root maps and supplementary files changed, whether the Domain Maps index changed, and any claim you removed as unverified or left open.
