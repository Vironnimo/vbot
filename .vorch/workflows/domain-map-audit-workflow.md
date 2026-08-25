# Domain Map Audit Workflow

A corpus-wide pass over all root domain maps and their supplementary routing structure: reduce default-loaded context without losing decision-relevant signal. For single-map work use `domain-map-workflow.md` - this audit builds on its placement, claim-verification, and supplementary-file rules and adds the cross-map view a single-map pass cannot provide.

Root maps are the always-read routing and safety layer. Supplementary files are task-gated depth. Audit the roots as a corpus; load supplementary contents only when a candidate, moved fact, or reference trigger makes that depth relevant.

## Procedure

1. **Inventory the routing structure** - List every root map under `.vorch/domain-maps/`, its `## References` triggers, and its supplementary files. Reconcile only root maps against the Domain Maps index in `.vorch/PROJECT.md`; supplementary files deliberately never get index rows. Flag a missing trigger target and a supplementary file with no owning root trigger as routing defects.
2. **Read the root corpus** - Read every root map and keep a compact ledger of domain ownership, boundaries, invariants, cross-domain contracts, gotchas, terms, and reference triggers. Do not preload or try to hold every supplementary file in context.
3. **Analyze across roots** - Record candidate edits:
   - **Duplication** - the same decision-relevant fact in two or more roots -> one owning home, a precise pointer or compact boundary statement in the rest.
   - **Misfiled** - a fact in the wrong domain -> move it to its documented owner.
   - **Consolidation** - scattered points that express one idea -> merge them into one statement.
   - **Boundary drift** - overlapping or contradictory responsibility across roots -> sharpen who owns what and what each side explicitly does not control.
   - **Staleness** - a claim that may no longer match implementation -> verify it against source/tests, then fix or remove it. Source remains the truth for implemented behavior; maps document routing, intended boundaries, invariants, and contracts.
   - **Term home** - a `## Terms` section is legitimate root-map content, never an inventory to cut. A domain-internal term belongs in its map; a cross-cutting or user-facing term belongs in `.vorch/GLOSSARY.md`. Keep one home and sweep stale pointers.
4. **Deepen only where routed** - Process affected domains one at a time. Load a supplementary file only when its trigger matches a candidate, when verifying a moved fact, or when auditing that reference's own scope. Apply `domain-map-workflow.md`'s different-feature test: always-relevant safety/routing stays in the root; task-specific contracts, procedures, transitions, and deep source guidance live in a supplement.
5. **Present the edit plan** - Before the first write, present one numbered corpus-level plan: what stays, moves, is removed, or becomes task-gated; which files change; and the canonical home of moved signal. Wait for approval unless the user has already explicitly approved the same scope and moves. Preserve or re-home every decision-relevant boundary, invariant, contract, gotcha, decision rule, term, and source pointer; low-value source mirroring, stale claims, and redundant prose may be removed.
6. **Apply domain by domain** - Immediately before the first domain-map write, read `domain-map-workflow.md` in full. Treat each affected domain as one unit: root, necessary supplements, adjacent-map pointers, and the root's `.vorch/PROJECT.md` index description when needed. Never add supplementary files to the Domain Maps index.
7. **Verify locally, then globally** - After each domain, re-read its changed files and diff, verify claims against source/tests, resolve every pointer, and confirm each trigger is sharp. Finish with a root-corpus sweep for stale headings/paths, broken or orphaned references, duplicated canonical facts, boundary contradictions, non-ASCII punctuation violating the domain-map workflow's ASCII-only rule, and supplementary rows accidentally added to the Domain Maps index.

Do not preserve a statement merely because it existed before the audit. The preservation target is decision-relevant signal, not exhaustive information; implementation details with no routing or safety value remain discoverable in source.

## Output

Report root maps created, updated, split, or removed; supplementary files created, updated, moved, or removed; cross-map signal deduplicated, moved, or consolidated; boundaries sharpened; stale claims fixed; root-index changes; verification performed; and anything flagged rather than changed.
