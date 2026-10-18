# Domain Map Audit Workflow

A corpus-wide pass that brings the root maps back within their size budget and repairs routing, duplication and boundaries across maps, without losing decision-relevant signal. Run it when `python scripts/domain_maps.py` reports `Corpus audit due` or when the user asks. It applies the placement, verification and removal rules of `domain-map-workflow.md` and adds the cross-map view a single-map change cannot provide.

The root corpus is too large for one context, so the audit works from compact ledgers: subagents read the maps, the auditing agent compares the ledgers. Where your environment offers no subagents, process the groups one after another and write each group's ledger to a scratch file before you read the next group.

## Procedure

1. **Measure.** Run `python scripts/domain_maps.py`. Its sizes and findings are the baseline. The target: every root map within budget, or a recorded reason why content every agent of that domain needs exceeds it; no routing or punctuation findings.
2. **Build ledgers.** Split the root maps into groups of at most 120,000 characters, keeping adjacent domains together, and give each group to a subagent with these instructions. For each root map, write a ledger of at most 3,000 characters:
   - responsibility and boundary in two or three lines;
   - contracts it defines that other domains use, and contracts of other domains it relies on;
   - invariants, terms and References triggers, one line each;
   - candidates, each with its section, size in characters and kind: task-gated (name the task that needs it), mirroring, duplicated (name the other map), misfiled (name the owner), stale (name the evidence), or delegated documentation (callers need it but the map leaves it to `--help` or probing).

   Subagents read a supplementary file only to check whether a candidate's content already lives there.
3. **Compare across maps.** From the ledgers, record:
   - **Duplication:** the same decision-relevant fact in two or more roots -> one owning home, a pointer or one-line boundary statement in the others.
   - **Misfiled:** a fact in the wrong domain -> move it to its owner.
   - **Boundary drift:** overlapping or contradictory responsibility -> state who owns what and what each side does not control.
   - **Buried cross-cutting mechanism:** a contract any domain may need to use but only one map documents -> cross-cutting home, `.vorch/PROJECT.md` pointer and back-references (`domain-map-workflow.md` -> What belongs where).
   - **Term home:** a domain-internal term belongs in its map's `## Terms`, a shared one in `.vorch/GLOSSARY.md`; one home, stale pointers swept. A `## Terms` section is root content, never an inventory to cut.
   - **Always-read layer:** `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md` against their rules in `AGENTS.md` (You maintain the docs, Terminology): content whose use is confined to one domain moves into that domain's map.
4. **Present the edit plan.** Before the first write, show the user one numbered plan: per domain, what moves where, what is deleted and why, which files change, and the expected root size afterwards. Wait for approval unless the user has already approved the same scope and moves.
5. **Apply domain by domain.** Give each affected domain, with its approved plan items, to a subagent that follows `domain-map-workflow.md`: root map, needed supplementary files, adjacent-map pointers and the Domain Maps index row. Subagents verify every fact they keep or move against source and tests, and report every removed fact with its new home or the reason it was deleted. Two subagents never edit the same file at the same time; pointer updates in another domain's map wait until that domain is done.
6. **Verify.** Check each subagent's removal report against its diff: read moved facts in their new homes. Run `python scripts/domain_maps.py` again and resolve every finding. Search the maps for pointers to headings that were renamed or removed (`-> <Heading>`).

Do not keep a statement merely because it existed before the audit. The preservation target is decision-relevant signal; implementation detail with no routing or safety value stays discoverable in source.

## Output

Report the root sizes before and after, root maps and supplementary files created, moved, split or removed, cross-map facts deduplicated or moved, boundaries sharpened, stale claims fixed, index changes, the verification performed, and anything flagged instead of changed.
