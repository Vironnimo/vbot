# Domain Map Audit Workflow

A corpus-wide pass over **all** domain maps: compress for token economy without losing information. For single-map work use `domain-map-workflow.md` — this builds on its rules (what to cut, the supplementary-file split, claim verification) and adds the cross-map view a single-map pass can't see.

## Procedure

1. **Inventory** — List every map under `.vorch/domain-maps/` and its supplementary files. Reconcile against the Domain Maps index in `.vorch/PROJECT.md`; note orphans and missing files.
2. **Read the corpus** — Read every map, skim supplementary files, hold the whole set in view at once.
3. **Cross-map analysis** — Record candidate edits:
   - **Duplication** — same fact in two+ maps → one owning home, a pointer (or nothing) in the rest.
   - **Misfiled** — fact in the wrong domain → move it to its owner.
   - **Consolidation** — scattered points that are one idea → merge into one statement.
   - **Boundary drift** — overlapping responsibility across maps → sharpen the boundary, state who owns what.
   - **Staleness** — claims that no longer match the code → verify, then fix or remove.
   - **Term home** — a `## Terms` section is legitimate map content, never an inventory to cut. But check placement: a domain-internal term sitting in `.vorch/GLOSSARY.md`, or a term duplicated in both the glossary and a map's `## Terms` → one home only (glossary for cross-cutting/user-facing, the map's `## Terms` for domain-internal); leave no copy behind and fix stale "GLOSSARY → <Term>" pointers.
4. **Per-map tightening** — Apply `domain-map-workflow.md`'s What To Cut, and extract task-gated material (procedures, deep references — needed only for one task) into `.vorch/domain-maps/<domain>/` with a sharp trigger line in the map's `## References`.
5. **Edit plan** — Present one numbered plan before editing: per change, what it is, files touched, where each moved or removed fact now lives. No fact may be dropped. Wait for approval.
6. **Apply & verify** — Edit, then re-read the changed maps and the diff: no fact lost, every pointer resolves, every trigger sharp, `.vorch/PROJECT.md` index current.

## Output

Report maps created / updated / split / removed, cross-map changes (deduped, moved, merged, boundaries sharpened), stale claims fixed, index changes, and anything flagged rather than changed.
