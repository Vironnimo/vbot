# Domain Map Audit Workflow

A corpus-wide pass that brings the root maps, `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md` back within their size budgets and repairs routing, duplication and boundaries across maps, without losing decision-relevant signal. Run it when `python scripts/domain_maps.py` reports `Corpus audit due` or when the user asks. It applies the rules of `domain-map-workflow.md` and adds the cross-map view a single-map change cannot provide.

The corpus is too large for one context, so subagents read the maps and write compact ledgers, and the auditing agent compares the ledgers. Where your environment offers no subagents, process the groups one after another and write each group's ledger to a scratch file before you read the next group. Work in a worktree (`AGENTS.md` -> Git) and commit at the end of each phase, so verification can diff against the commit the audit started from (the base).

## Procedure

1. **Measure.** Run `python scripts/domain_maps.py`. Its sizes and findings are the baseline. The target: every root map, `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md` within budget, or a recorded reason why content every agent of that domain needs exceeds it; no routing or punctuation findings.
2. **Build ledgers.** Split the root maps into groups of at most 120,000 characters, keeping adjacent domains together, and give each group to a subagent. For each root map it writes a ledger of at most 3,000 characters:
   - responsibility and boundary in two or three lines;
   - contracts it defines that other domains use, and contracts of other domains it relies on;
   - invariants, terms and References triggers, one line each;
   - candidates, each with its section, size in characters and kind: task-gated (name the task that needs it), mirroring, duplicated (name the other map), misfiled (name the owner), stale (name the evidence), delegated documentation (callers need it but the map leaves it to `--help` or probing), term creep (a Terms entry describing mechanics), or spread term (a Terms entry other domains use; name them).

   Subagents read a supplementary file only to check whether a candidate's content already lives there.
3. **Compare across maps.** From the ledgers, record:
   - **Duplication:** the same decision-relevant fact in two or more roots -> one owning home, a pointer or one-line boundary statement in the others.
   - **Misfiled:** a fact in the wrong domain -> move it to its owner.
   - **Boundary drift:** overlapping or contradictory responsibility -> state who owns what and what each side does not control.
   - **Buried cross-cutting mechanism:** a contract any domain may need to use but only one map documents -> cross-cutting home, `.vorch/PROJECT.md` pointer and back-references (`domain-map-workflow.md` -> What belongs where).
   - **Term home:** a domain-internal term belongs in its map's `## Terms`, a shared one in `.vorch/GLOSSARY.md`; one home, stale pointers swept. A Terms entry that several domains use moves to the glossary; a glossary term only one domain uses moves to that map. A `## Terms` section is root content, never an inventory to cut.
   - **Term creep:** an entry, in the glossary or a map's Terms, that has grown beyond meaning and distinction (`AGENTS.md` -> Terminology): storage, states, limits, syntax or code names -> keep the meaning, move the mechanics into the owning section, which the entry names.
   - **Always-read layer:** `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md` against their rules in `AGENTS.md` (You maintain the docs, Terminology): content whose use is confined to one domain moves into that domain's map.
4. **Present the edit plan.** Before the first write, show the user one numbered plan: per domain, what moves where, what is deleted and why, which files change, and the expected root size afterwards. Wait for approval unless the user has already approved the same scope and moves.
5. **Apply in disjoint groups.** Give each subagent a group of domains with their approved plan items. It follows `domain-map-workflow.md`, edits only its group's root maps and supplementary files, and verifies every fact it keeps or moves against source and tests. It returns:
   - a removal report: every removed fact with its new home or the reason it was deleted;
   - handoffs for edits that belong in files outside its group (an inserted fact, a repaired pointer, an index row): target file, anchor and exact text.

   Then apply the handoffs in a second round, partitioned by target file so that no two subagents edit the same file; one of them takes `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md`.
6. **Verify with fresh subagents** that did not write the changes, one per group:
   - check every removal-report entry against `git diff <base>..HEAD`: a moved fact is in its new home, a deleted one was stale, mirroring, or is stated by its owner;
   - pick about 10 decision-relevant facts per root map from its base version (`git show <base>:<path>`) and confirm each is still findable; this catches losses no report mentions.

   Restore every loss in its proper home; when a restore pushes a root map above budget, move task-gated material of that map into a supplementary file instead of dropping the fact. Then run `python scripts/domain_maps.py`, resolve every finding, and sweep for pointers to moved, renamed or removed headings (`domain-map-workflow.md` -> Every map change, step 6).

Do not keep a statement merely because it existed before the audit. The preservation target is decision-relevant signal; implementation detail with no routing or safety value stays discoverable in source.

## Output

Report the root and PROJECT.md sizes before and after, root maps and supplementary files created, moved, split or removed, cross-map facts deduplicated or moved, boundaries sharpened, stale claims fixed, index changes, the verification performed with the losses it found and restored, and anything flagged instead of changed.
