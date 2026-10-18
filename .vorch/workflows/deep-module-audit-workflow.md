# Deep-Module Audit Workflow

A corpus-wide pass that measures the code against vBot's architecture rules, reports the problems that matter, and, once the user approves a plan, refactors them in worktrees and has fresh subagents verify the result. Run it when the user asks, over the whole repository or a scope the user names.

The rules are `AGENTS.md` -> Architecture & code and `.vorch/PROJECT.md` -> Architecture (layers, the per-file soft limit, how to split an oversized file); the root maps state each domain's intended owner and boundaries. The audit applies these rules and sets no new ones. Maps state intent; source and tests show what is implemented.

The code is too large for one context: subagents gather and confirm candidates per group and write compact ledgers, and the auditing agent compares them. Where your environment offers no subagents, process the groups one after another and write each ledger to a scratch file before you start the next group.

## Dimensions

1. **Oversized files:** a source file over the soft limit. Every one gets either the internal units a typical edit would read on their own and a split into them under the same owner, or a reason to defer it, such as a data catalog that is only edited entry by entry. Cohesion is not such a reason. Never split into arbitrary chunks, method-binding tables or compressed formatting.
2. **Shallow modules:** a public surface nearly as large as its implementation, mostly delegation, a thin wrapper, a main file that only re-exports. Keep it when it is a deliberate seam that hides a real choice, such as a Protocol boundary or an injection point. Cure: fold it into its caller, or deepen it until it earns its interface.
3. **Duplicate logic:** one concern implemented in two places (twin functions, copied literals, parallel helpers). Keep it when the likeness is superficial and joining would couple unrelated things. Cure: one owner, named.
4. **Boundary violations:** code or imports crossing a layer or domain line, such as business logic in `server/` or `webui/`, an accessor importing what it must not, a domain reaching into another's internals, or I/O outside its owner. Keep it when the root map explicitly allows it. Cure: move the code to its owner; when source, tests and user decisions show that the map is stale, the finding is documentation drift instead.
5. **Unjustified modules:** a package that no domain map owns, or several small modules doing what one owner could. Name the existing owner that could take it and why it cannot; "no owner fits" is an answer, "not checked" is not. Cure: fold it into the owner, or record the justification in the owning domain map. Supplementary map files are documentation, never modules.
6. **Over-exposure:** callers importing another module's private `_` files or deep paths instead of its main-file API, or a main file exporting internals. Cure: narrow the interface and route the callers through it.
7. **Missing types:** values that travel together without a type of their own: the same group of three or more parameters in at least three functions or across a module boundary, the same dict or tuple shape built and read in several places, or a bool parameter that switches a function between two behaviors. `python -m ruff check <paths> --select PLR0913,FBT001 --exit-zero` lists long parameter lists and bool parameters as candidates; a long parameter list alone is no finding. Keep it when the values pass only between two functions, or when the type would be a bag of fields with no meaning of its own. Cure: a type owned by the domain that owns the concept, with the logic that works on those values moved into it; for a switching bool, two functions or an enum.

## Judging a candidate

- A finding needs evidence in source: `file:line`, the callers or imports involved, and the rule it breaks. A pattern match alone is a candidate, not a finding.
- Recommend a cure only when its risk and churn are smaller than the problem; otherwise drop the candidate and name it in the ledger with that reason.
- Classify each finding as code or documentation drift.
- Severity: 🔴 hurts work now (many callers, recurring bugs, every edit in the area), 🟡 real, schedule it, 🔵 opportunistic. Oversized files carry no severity; they form their own list.

Report only findings that survive step 4 of the procedure; three real problems beat twenty suspicions.

## Procedure

1. **Partition.** List the tracked source files in scope outside the test directories with their line counts. Group related domains along the Domain Maps index into groups of at most about 50,000 lines.
2. **Gather ledgers.** Give each group to a subagent. It reads this workflow, the rules above and the group's root maps (a supplementary file only when its trigger matches a candidate), inspects the group for all dimensions, and confirms each candidate against source and tests. It changes no files and returns a ledger of at most 4,000 characters:
   - per domain, its public interface and boundary in one or two lines;
   - each finding: dimension, `file:line`, evidence, code or documentation drift, severity, cure, and the files the cure touches;
   - each oversized file: line count and proposed units, or the reason to defer;
   - leads into other groups: a suspected duplicate or boundary crossing, naming the other side.
3. **Compare.** Resolve each lead by reading both sides, merge findings reported from both ends, and drop leads that do not hold.
4. **Challenge.** Give each group's 🔴 and 🟡 findings to a fresh subagent that tries to refute them against source and tests. Drop or downgrade what it refutes.
5. **Present the report and plan** (Output below) and wait for the user's approval of all, some or none of the items. No file has changed up to here.
6. **Implement in worktrees.** Split the approved items into implementation groups with disjoint files; items that touch the same files go into one group, in order. For each group, create a worktree with `python scripts/worktree.py create <name>` and give it to one subagent, which:
   - refactors without changing behavior, public contracts, persisted formats or Tool-visible behavior unless its plan item says so;
   - moves tests with the code to the new owner's public interface and deletes tests the change makes redundant (`AGENTS.md` -> Testing);
   - updates the affected domain maps (`domain-map-workflow.md`);
   - commits once the tests covering its change pass, and returns a change report: per plan item, what moved where, which public names were removed and where their callers went, which tests moved or were deleted, and what it did not do.
7. **Verify with fresh subagents** that did not write the changes, one per implementation group. Each checks the worktree's diff against its plan items and change report: every caller of a moved or removed name is updated, behavior changed only where the plan says so, every deleted test's behavior is asserted elsewhere or no longer exists, no pass-through or dead code is left behind, oversized files reached their target, and the maps match the code. Where a behavior claim is in doubt, it runs a targeted probe or the affected tests. The writing subagent fixes what it finds; verify again until nothing is left.
8. **Merge** each verified worktree with `python scripts/worktree.py merge <name>`, one after another; for conflicts, follow `worktree-workflow.md`. Then run `python scripts/domain_maps.py` and resolve its findings.

## Output

The report and plan in step 5:

```markdown
## Deep-Module Audit: <scope>

### Summary
[One paragraph: overall architectural health and the few things that matter most.]

### Findings
F1. **[severity] [code | documentation drift] <dimension>, `file:line`:** [what is wrong, the evidence, the rule it breaks]. [The cure and the files it touches.]

### Oversized files
S1. `path` (N lines): [split into <units> | defer: <reason>]

### Plan
[The implementation groups: which F and S items each takes, its files, and the merge order.]
```

The final report after step 8: per plan item, whether it was implemented and, if not, why; what verification found and how it was fixed; test failures the changes cannot explain, left for the push. Approved items that were not implemented go to `.vorch/FLAGGED.md`.
