# Spec Workflow

Use this workflow when creating, auditing, or updating `.vorch/specs/<domain>.md`.

Specs are factual working notes for agents. They are not architecture documentation, not generated API reference, and not a line-count contest.

A good spec is:
- Short enough to stay readable
- Factual enough to trust
- Complete enough to keep agents from touching the wrong layer

Shorter is useful only when it removes noise. Do not remove high-signal behavior, contracts, invariants, gotchas, decision rules, or source-of-truth pointers just because they duplicate code or may change later. If behavior changes, update the spec with the behavior.

## Ownership

The Orchestrator creates and maintains specs. All other agents read them.

If source verification is needed and your role must not read source code directly, delegate bounded exploration to `explorer` or use Builder/Reviewer findings that include concrete source/test references. Do not write factual claims from memory or intuition.

## Read First

Before spec work:
1. Read `AGENTS.md`, `.vorch/PROJECT.md`, and `.vorch/GLOSSARY.md` as required by the system.
2. Read this workflow in full.
3. Read the current target spec if it exists.
4. Read related specs when boundaries or contracts cross domains.

## What Belongs In A Spec

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

## Verify Claims

Every factual claim should be backed by one of:
- Source code
- Tests
- Existing specs or `.vorch/PROJECT.md`
- An explicit user/project decision

If a statement cannot be backed, either remove it or rewrite it as a convention/policy that the Orchestrator is intentionally establishing.

For doc-only spec work, do not run application tests unless the user asks or application/test code also changed. Verify by reading source-of-truth evidence, adjacent specs, and the diff.

## Creating A Spec

Use when a new domain emerges or an existing domain has no spec.

1. Identify the domain boundary. A domain is any module or subsystem with a clear responsibility where working without context risks misunderstanding interfaces, ownership, or conventions.
2. Choose the spec path: `.vorch/specs/<domain>.md`. Use nested paths only when a child domain has enough independent contracts or gotchas that a separate spec improves agent handoff.
3. Gather evidence from `.vorch/PROJECT.md`, related specs, user decisions, and source/test verification.
4. Write only the sections that apply. There is no required minimum.
5. Add the new spec to the Specs index in `.vorch/PROJECT.md`.
6. Keep the first version useful, not exhaustive. Add more only when it prevents likely mistakes.

## Maintaining A Spec

Use when implementation changes a domain, a Builder/Reviewer reports project-doc impact, a domain boundary changes, or an existing spec is stale, noisy, misleading, or incomplete.

For routine maintenance during implementation:
1. Make the narrow factual update needed by the completed work.
2. Base the change on Builder/Reviewer output, explorer summaries, source/test evidence, or explicit user decisions.
3. Update the Specs index if a spec was created, renamed, split, or removed.

For dedicated spec cleanup or audit:
1. Read the current spec and identify what each section is trying to help agents do.
2. Verify kept factual claims against source/test evidence or explicit decisions.
3. Present a numbered edit plan before judgment-heavy rewrites: what stays, what is removed, what is added, and what moves.
4. After approval, edit the spec.
5. Re-check the remaining claims and the diff. The work is done only when the spec is useful for agents and factually correct.

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
```

## Output

When spec work is complete, report:
- Which spec files were created or updated
- Whether `.vorch/PROJECT.md` Specs index changed
- Any assumptions, unverified claims removed, or open questions
