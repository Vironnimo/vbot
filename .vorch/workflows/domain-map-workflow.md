# Domain Map Workflow

Use this workflow when creating, auditing, or updating domain maps and supplementary files under `.vorch/domain-maps/`.

Domain maps are factual working notes for agents. They are the always-read routing and safety layer for a domain: they provide the context needed across work in that domain and point to task-gated depth. They are not architecture documentation, not generated API reference, not the container for every verified fact about the domain, and not a line-count contest.

A good domain map is:
- Short enough to stay readable
- Factual enough to trust
- Complete enough to keep agents from touching the wrong layer

Shorter is useful only when it removes noise. Do not lose high-signal behavior, contracts, invariants, gotchas, decision rules, or source-of-truth pointers merely to shorten a file. Preserving signal does not mean keeping it in the root map: keep always-relevant context there, move task-gated depth to a supplementary file, and replace repeated explanations with a precise pointer to their canonical documented owner when repetition adds no decision value. Source duplication alone is not a reason to remove decision-relevant semantics. If behavior changes, update its canonical documented home.

## Ownership

The agent doing the work maintains the affected maps and supplementary files as part of the task. No separate Orchestrator or documentation role is required.

Verify implementation claims against source and relevant tests. Existing findings are usable when their concrete references can be checked. Do not write factual claims from memory or intuition.

## Read First

Before domain-map work:
1. Load core context according to `AGENTS.md` -> Load context for the task.
2. Read this workflow in full unless its current contents are already in context.
3. Read the current target map if it exists.
4. Read related maps when boundaries or contracts cross domains.

## What Belongs In A Domain Map

Keep information that helps agents choose the right file, layer, abstraction, or test across work in the domain:
- Domain responsibility and boundary: what this domain owns and what it does not own
- Cross-domain touchpoints: which other domains depend on it or feed it
- Cross-task contracts other code relies on and agents need to recognize the correct boundary: public functions, events, API shapes, storage formats, message payloads, return shapes
- Invariants: rules that must stay true, such as "only one active run per session"
- Decision rules: "fix this here, not there" guidance
- Domain-specific conventions beyond `AGENTS.md`
- Constraints and gotchas: non-obvious behavior, fragile areas, previous failure modes, security or performance traps
- Source-of-truth pointers when they help agents verify or extend behavior quickly
- Cross-cutting mechanisms whose *use trigger* can arise in any domain while the contract lives in one place (example: kernel-to-model notifications). Such a mechanism needs its own cross-cutting home, a one-line always-loaded pointer in `.vorch/PROJECT.md` -> Context, and one-line back-references from each origin domain's References index - otherwise agents working outside the owning domain never learn it exists.

These are eligible kinds of map content, not a requirement to place every instance in the root map. A contract can be important and still be task-gated; exact feature payloads, state transitions, and operational sequences belong in a supplementary file when agents working on other features in the domain do not need them.

## What To Cut

Cut information that slows agents down without making them safer:
- Exhaustive RPC, method, field, settings-key, or schema inventories when callers do not need them
- Field-by-field code mirroring that adds no decision value
- Per-component UI inventories that do not guide implementation
- Global rules already stated in `AGENTS.md`
- Architecture prose that does not affect how an agent should work in the domain
- Sections that do not apply

Do not discard important behavior, field semantics, output contracts, or gotchas merely because they duplicate code. Move task-gated signal to a supplementary file, and replace cross-map repetition with a pointer to the canonical owner. Short but wrong is worse than long.

**Mirroring vs operational knowledge.** An inventory is a cut candidate only when it restates what the artifact reliably tells its caller at runtime. Semantics a caller must know before or without running the artifact - flag consequences, exit-code behavior, security rules, offline behavior - are decision-relevant signal: never delegate documented content to `--help`, live probing, or source diving, because the agent's task may be repairing exactly the artifact it would have to execute. When consolidating instructions, preserve an explicit route to the required context or gate. Keep task-specific read triggers where they guide the work; avoid repeating global rules already loaded through `AGENTS.md`.

## Formatting: plain ASCII punctuation only

Domain maps use plain ASCII punctuation exclusively. Use `->` for pointers, `-` for dashes, `...` for ellipses, `>=` / `<=` / `!=` / `~` for comparisons, and straight quotes. Do not use typographic characters: no em/en dashes, Unicode arrows, curly quotes, mathematical glyphs, or emoji. The only exception is a literal product string quoted from the UI (a button label, a rendered display fragment); such content stays verbatim. Reason: agents and tools match, diff, grep, and edit these files as plain text, and visually similar Unicode variants silently break exact-text matching and copy-pasted references.

## Domain Terms (the `## Terms` section)

A domain map's `## Terms` section defines vocabulary needed within that domain. Shared terms needed to interpret vBot across tasks live in `.vorch/GLOSSARY.md`; placement and routine maintenance follow `AGENTS.md` -> Terminology. Omit the section when there are no domain-specific terms.

- **What belongs here:** project-specific meanings and useful distinctions needed only within this domain, even when the user mentions them. Shared vocabulary that prevents misreading vBot across tasks belongs in `.vorch/GLOSSARY.md`. Keep one canonical home per term; when moving an entry, remove its old definition and repair references.
- **Placement:** put `## Terms` after Overview, before detailed sections, so readers encounter necessary vocabulary first.
- **Format:** one `### <Term>` per entry with a concise definition, usually one or two sentences. Include a distinction from nearby terms only when it prevents likely confusion; `Definition:` / `Not:` labels are optional. Where applicable, open with one line pointing to relevant shared terms (e.g. "Core terms (Provider, Model) live in `.vorch/GLOSSARY.md`").
- **Cross-references:** name a term in another map plainly ("see `models.md`"); point at a core term as "GLOSSARY -> <Term>". When a term leaves the glossary, sweep the maps for stale "GLOSSARY -> <Term>" pointers to it.

## References & Supplementary Files

A domain map is the always-read orientation layer for its domain. Material needed only for a specific task does not belong in it - split it into a supplementary file so reading the map stays cheap.

**Layout.** The map stays the entry point as a loose file: `.vorch/domain-maps/<domain>.md`. When a domain needs depth, give it a sibling folder of the same name and put the supplementary files there:

```
domain-maps/
  providers.md          <- the map (always read when working in the domain)
  providers/            <- supplementary files (read on demand)
    add-a-provider.md
    endpoint-probing.md
```

**What to split out - decide by relevance per task, not by size:**
- **Keep in the map:** what an agent needs to safely touch *anything* in the domain - boundaries, cross-task contracts, invariants, decision rules, gotchas.
- **Split into a file:** *task-gated* material - feature-specific behavior and contracts, exact payload or state-transition detail, recovery matrices, step-by-step procedures ("how to add a provider"), and deep references (exhaustive endpoint catalogs, probing recipes) - needed only for that feature or task family.

The test: *would an agent working on a different feature in this domain still need this to choose the correct owner/layer or avoid violating a domain-wide invariant?* Yes -> map. No -> supplementary file. Importance does not decide placement: a critical task-specific recovery contract still belongs in its task-gated reference. A long map is a prompt to look for task-gated content to extract - never split content that is always needed just to shorten the file; that only forces an extra read.

**Linking.** The map carries a small index near the end. Each entry is a *trigger*, not a title - it tells an agent when to pull the file without opening it:

```markdown
## References

Read these only when your task matches - not by default.

- Adding or changing a provider -> `providers/add-a-provider.md`
- Probing endpoints / verifying a provider's API -> `providers/endpoint-probing.md`
```

Keep triggers sharp. The failure mode is an agent missing context because a trigger was vague - a sloppy split is worse than none.

**Index discriminator.** Supplementary files are reached only through their map's References index. They are never listed in the Domain Maps index in `.vorch/PROJECT.md` - that index lists maps (domains) only. If a child area earns its own index entry, it is a domain map in its own right, not a supplementary file.

## Verify Claims

Every factual claim should be backed by one of:
- Source code
- Tests
- Existing maps or `.vorch/PROJECT.md`
- An explicit user/project decision

Distinguish implementation observations from explicit requirements, engineering contracts, and proposals (`AGENTS.md` -> Interpret documentation). Code shows what is implemented, not whether it satisfies the intended behavior. Correct stale observations; investigate conflicts with requirements rather than silently rewriting those requirements. Remove unsupported claims or mark them as unresolved/proposed when useful; never turn an unverified claim into a policy merely to keep it.

For doc-only map work, do not run application tests unless the user asks or application/test code also changed. Verify by reading source-of-truth evidence, adjacent maps, and the diff.

## Creating A Domain Map

Use when a new domain emerges or an existing domain has no map.

1. Identify the domain boundary. A domain is any module or subsystem with a clear responsibility where working without context risks misunderstanding interfaces, ownership, or conventions.
2. Choose the map path: `.vorch/domain-maps/<domain>.md`. Use nested paths only when a child domain has enough independent contracts or gotchas that a separate map improves agent handoff.
3. Gather evidence from `.vorch/PROJECT.md`, related maps, user decisions, and source/test verification.
4. Write only the sections that apply. There is no required minimum. Keep task-gated procedures and deep references out of the map - put them in supplementary files (see References & Supplementary Files).
5. Add the new map to the Domain Maps index in `.vorch/PROJECT.md`.
6. Keep the first version useful, not exhaustive. Add more only when it prevents likely mistakes.

## Maintaining A Domain Map

Use when work affects documented behavior, ownership, or contracts, or an existing map is stale, noisy, misleading, incomplete, or contains task-gated detail that belongs in supplementary files.

For routine maintenance during implementation:
1. Decide placement before writing: apply the different-feature test from References & Supplementary Files. Always-relevant context updates the map; task-gated context updates a supplementary file.
2. Update one canonical documented home. When a supplementary file is created or its task scope changes, add or sharpen its trigger in the map's References index.
3. Make the narrow factual update needed by the completed work.
4. Base changes on source/test evidence, verifiable findings, or explicit user decisions.
5. Update the Domain Maps index if a map was created, renamed, split, or removed. Supplementary files never enter that index.
6. After slimming, diff old against new and account for every removed fact: point to its verified new home (verified by reading it, not assumed) or record why it was safe to delete outright. Establish path/reference resolution conventions before the document's first bare reference.

For dedicated map cleanup or audit:
1. Read the current map and identify what each section is trying to help agents do.
2. Verify kept factual claims against source/test evidence or explicit decisions.
3. For substantial or judgment-heavy rewrites, briefly explain what stays, changes, or moves, including task-gated material. Scale discussion to uncertainty and consequences; routine factual maintenance needs no separate planning round.
4. Edit within the user's authorized scope. Existing authorization is sufficient; ask only when unresolved choices would materially change meaning or scope.
5. Re-check the remaining claims and the diff. The work is done only when the map is useful for agents and factually correct.

## Template

Use this as a starting point. Remove every section that does not apply.

```markdown
# <Domain Name>

<One sentence: what this domain does and where it sits in the system.>

## Overview

[What this domain is responsible for. What it owns. What it does not do if non-obvious. Keep it to the context an agent needs before touching the domain.]

## Terms

[Domain-local vocabulary, one `### Term` with a concise definition and useful distinctions. Place after Overview. Shared vocabulary needed across tasks lives in `.vorch/GLOSSARY.md`; user mentions do not determine placement. Omit if there are no domain-specific terms.]

## Data Model

[Only include if this domain owns entities or persisted data. Name the key entities, shapes, relationships, and invariants agents need across work in the domain. Move feature-specific fields and detailed schema inventories to a task-gated reference.]

## Interfaces

[Cross-task contracts other parts of the system depend on: exported functions/classes/hooks, API boundaries, event families, message or storage formats, and return-shape invariants. Focus on what agents need to find the correct owner and preserve domain-wide behavior; move endpoint-, event-, and payload-level detail used only by one feature or task family to a task-gated reference.]

## Conventions

[Patterns specific to this domain that go beyond global rules: error handling, naming, async behavior, extension patterns, testing patterns, ownership boundaries.]

## External Dependencies

[Only include if this domain owns or calls external services, APIs, SDKs, databases, or infrastructure. Note auth, limits, quirks, or failure behavior relevant for development.]

## Constraints & Gotchas

[Non-obvious behavior, limitations, fragile areas, previous bugs, security/performance traps, and things that look safe to change but are not.]

## References

[Only if the domain has supplementary files. One trigger line per file - when to read it, not just its title.]
- <When an agent should read it> -> `<domain>/<file>.md`
```

## Output

When domain-map work is complete, report:
- Which domain maps were created or updated
- Which supplementary files were created or updated, if any
- Whether `.vorch/PROJECT.md` Domain Maps index changed
- Any assumptions, unverified claims removed, or open questions
