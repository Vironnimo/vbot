# Domain Map Workflow

Use this workflow when creating, auditing, or updating `.vorch/domain-maps/<domain>.md`.

Domain maps are factual working notes for agents. They are not architecture documentation, not generated API reference, and not a line-count contest.

A good domain map is:
- Short enough to stay readable
- Factual enough to trust
- Complete enough to keep agents from touching the wrong layer

Shorter is useful only when it removes noise. Do not remove high-signal behavior, contracts, invariants, gotchas, decision rules, or source-of-truth pointers just because they duplicate code or may change later. If behavior changes, update the map with the behavior.

## Ownership

The Orchestrator creates and maintains domain maps. All other agents read them.

If source verification is needed and your role must not read source code directly, delegate bounded exploration to `explorer` or use Builder/Reviewer findings that include concrete source/test references. Do not write factual claims from memory or intuition.

## Read First

Before domain-map work:
1. Read `AGENTS.md`, `.vorch/PROJECT.md`, and `.vorch/GLOSSARY.md` as required by the system.
2. Read this workflow in full.
3. Read the current target map if it exists.
4. Read related maps when boundaries or contracts cross domains.

## What Belongs In A Domain Map

Keep information that helps agents choose the right file, layer, abstraction, or test:
- Domain responsibility and boundary: what this domain owns and what it does not own
- Cross-domain touchpoints: which other domains depend on it or feed it
- Contracts other code relies on: public functions, events, API shapes, storage formats, message payloads, return shapes
- Invariants: rules that must stay true, such as "only one active run per session"
- Decision rules: "fix this here, not there" guidance
- Domain-specific conventions beyond `AGENTS.md`
- Constraints and gotchas: non-obvious behavior, fragile areas, previous failure modes, security or performance traps
- Source-of-truth pointers when they help agents verify or extend behavior quickly

## What To Cut

Cut information that slows agents down without making them safer:
- Exhaustive RPC, method, field, settings-key, or schema inventories when callers do not need them
- Field-by-field code mirroring that adds no decision value
- Per-component UI inventories that do not guide implementation
- Global rules already stated in `AGENTS.md`
- Architecture prose that does not affect how an agent should work in the domain
- Sections that do not apply

Do not cut important behavior, field semantics, output contracts, or gotchas merely because they duplicate code. Short but wrong is worse than long.

## References & Supplementary Files

A domain map is the always-read orientation layer for its domain. Material needed only for a specific task does not belong in it — split it into a supplementary file so reading the map stays cheap.

**Layout.** The map stays the entry point as a loose file: `.vorch/domain-maps/<domain>.md`. When a domain needs depth, give it a sibling folder of the same name and put the supplementary files there:

```
domain-maps/
  providers.md          ← the map (always read when working in the domain)
  providers/            ← supplementary files (read on demand)
    add-a-provider.md
    endpoint-probing.md
```

**What to split out — decide by relevance per task, not by size:**
- **Keep in the map:** what an agent needs to safely touch *anything* in the domain — boundaries, contracts, invariants, decision rules, gotchas.
- **Split into a file:** *task-gated* material — step-by-step procedures ("how to add a provider"), deep references (exhaustive endpoint catalogs, probing recipes) — needed only when doing that specific task.

The test: *does an agent need this to work safely in the domain at all, or only for this one task?* Always → map. One task → supplementary file. A long map is a prompt to look for task-gated content to extract — never split content that is always needed just to shorten the file; that only forces an extra read.

**Linking.** The map carries a small index near the end. Each entry is a *trigger*, not a title — it tells an agent when to pull the file without opening it:

```markdown
## References

Read these only when your task matches — not by default.

- Adding or changing a provider → `providers/add-a-provider.md`
- Probing endpoints / verifying a provider's API → `providers/endpoint-probing.md`
```

Keep triggers sharp. The failure mode is an agent missing context because a trigger was vague — a sloppy split is worse than none.

**Index discriminator.** Supplementary files are reached only through their map's References index. They are never listed in the Domain Maps index in `.vorch/PROJECT.md` — that index lists maps (domains) only. If a child area earns its own index entry, it is a domain map in its own right, not a supplementary file.

## Verify Claims

Every factual claim should be backed by one of:
- Source code
- Tests
- Existing maps or `.vorch/PROJECT.md`
- An explicit user/project decision

If a statement cannot be backed, either remove it or rewrite it as a convention/policy that the Orchestrator is intentionally establishing.

For doc-only map work, do not run application tests unless the user asks or application/test code also changed. Verify by reading source-of-truth evidence, adjacent maps, and the diff.

## Creating A Domain Map

Use when a new domain emerges or an existing domain has no map.

1. Identify the domain boundary. A domain is any module or subsystem with a clear responsibility where working without context risks misunderstanding interfaces, ownership, or conventions.
2. Choose the map path: `.vorch/domain-maps/<domain>.md`. Use nested paths only when a child domain has enough independent contracts or gotchas that a separate map improves agent handoff.
3. Gather evidence from `.vorch/PROJECT.md`, related maps, user decisions, and source/test verification.
4. Write only the sections that apply. There is no required minimum. Keep task-gated procedures and deep references out of the map — put them in supplementary files (see References & Supplementary Files).
5. Add the new map to the Domain Maps index in `.vorch/PROJECT.md`.
6. Keep the first version useful, not exhaustive. Add more only when it prevents likely mistakes.

## Maintaining A Domain Map

Use when implementation changes a domain, a Builder/Reviewer reports project-doc impact, a domain boundary changes, or an existing map is stale, noisy, misleading, incomplete, or large enough that task-gated content should move to supplementary files.

For routine maintenance during implementation:
1. Make the narrow factual update needed by the completed work.
2. Base the change on Builder/Reviewer output, explorer summaries, source/test evidence, or explicit user decisions.
3. Update the Domain Maps index if a map was created, renamed, split, or removed.

For dedicated map cleanup or audit:
1. Read the current map and identify what each section is trying to help agents do.
2. Verify kept factual claims against source/test evidence or explicit decisions.
3. Present a numbered edit plan before judgment-heavy rewrites: what stays, what is removed, what is added, and what moves — including any task-gated content that moves to a supplementary file.
4. After approval, edit the map.
5. Re-check the remaining claims and the diff. The work is done only when the map is useful for agents and factually correct.

## Template

Use this as a starting point. Remove every section that does not apply.

```markdown
# <Domain Name>

<One sentence: what this domain does and where it sits in the system.>

## Overview

[What this domain is responsible for. What it owns. What it does not do if non-obvious. Keep it to the context an agent needs before touching the domain.]

## Data Model

[Only include if this domain owns entities or persisted data. Name key entities, shapes, important fields, relationships, and invariants that other domains depend on.]

## Interfaces

[Contracts other parts of the system depend on: exported functions/classes/hooks, API endpoints, event shapes, message formats, storage formats, return shapes. Focus on what callers need to know.]

## Conventions

[Patterns specific to this domain that go beyond global rules: error handling, naming, async behavior, extension patterns, testing patterns, ownership boundaries.]

## External Dependencies

[Only include if this domain owns or calls external services, APIs, SDKs, databases, or infrastructure. Note auth, limits, quirks, or failure behavior relevant for development.]

## Constraints & Gotchas

[Non-obvious behavior, limitations, fragile areas, previous bugs, security/performance traps, and things that look safe to change but are not.]

## References

[Only if the domain has supplementary files. One trigger line per file — when to read it, not just its title.]
- <When an agent should read it> → `<domain>/<file>.md`
```

## Output

When domain-map work is complete, report:
- Which domain maps were created or updated
- Which supplementary files were created or updated, if any
- Whether `.vorch/PROJECT.md` Domain Maps index changed
- Any assumptions, unverified claims removed, or open questions
