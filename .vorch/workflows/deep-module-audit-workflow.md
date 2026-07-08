# Deep-Module Audit Workflow

A corpus-wide pass that checks the repo against the deep-module philosophy and returns a prioritized findings report — each finding with a concrete recommended refactor. It is **diagnose-only**: the audit run never rewrites code.

Run **on user request only** (e.g. "run the deep-module audit"). The Orchestrator owns it and may delegate per-dimension candidate-gathering to the **explorer**.

## Source of truth

The philosophy and its boundaries are already written down — the audit measures the repo against them, it does not invent standards:

- **Deep-module principles** — `AGENTS.md` (Architecture): few deep modules, extend before adding, a module is too shallow when its interface is nearly as large as its implementation, when it's mostly pass-through, when it wraps something without adding an abstraction, or when callers must know its internals.
- **The declared module map & layers** — `.vorch/PROJECT.md`: the module/architecture overview, the layer rules, and any file-size soft limit it sets.
- **The declared domain boundaries** — the domain maps under `.vorch/domain-maps/`. These are the **oracle** for "does this code belong where it lives": a boundary violation is code whose behavior contradicts its domain's *stated* boundary, not a reviewer's hunch.

## Diagnose, never execute

Every finding carries a recommended refactor — that is the point of the audit. But the audit run itself changes no code: even an obvious duplication is written up as a finding, not fixed, so the report stays a faithful, complete picture of the repo. Remediation goes through the normal pipeline (Step 4 onward) so each refactor is planned, scoped, tested, and reviewed.

## Dimensions

Each dimension = a cheap signal to find candidates + a judgment rule to confirm (with its "legitimate when…" escape) + the remediation the finding recommends. **Size and similarity are signals, never the finding itself.**

1. **Oversized files** — *Signal:* files at/over the file-size soft limit `PROJECT.md` sets, or far larger than their siblings. *Judge:* one deep responsibility that is legitimately large, or several responsibilities bundled? *Legitimate when:* a single cohesive capability that would only fragment if split. *Recommend:* split by responsibility into a deeper owner, or record why it stays.
2. **Shallow modules** — *Signal:* a module/file whose public surface is nearly as large as its implementation; mostly delegation/pass-through; a thin wrapper; a folder-module whose main file just re-exports. *Judge:* does it hide complexity / add an abstraction, or is it a relay callers could skip? *Legitimate when:* it's a deliberate seam (a Protocol boundary, an injection point) that hides a real choice. *Recommend:* fold it back into the caller, or deepen it until it earns its interface.
3. **Duplicate / near-duplicate logic** — *Signal:* repeated blocks, parallel implementations of the same concern across files or domains (recurring names, copied literals, twin functions). *Judge:* one concept with two homes, or coincidental shape? *Legitimate when:* the similarity is superficial and coupling them would marry unrelated things. *Recommend:* extract to a single owner (name it).
4. **Domain boundary violations (outliers)** — *Signal:* logic or imports crossing a declared layer/domain line — business logic in the transport/UI layer, an accessor importing project code it must not, a domain reaching into another domain's internals, data/IO outside its owning layer. Checked against `PROJECT.md` layers + the relevant domain map. *Judge:* does this code's behavior belong to the domain it lives in, per the map's stated boundary? *Legitimate when:* the map explicitly allows it (then the finding is against the map, not the code — see the last anti-noise rule). *Recommend:* move it to its owning domain.
5. **Module sprawl / unjustified modules** — *Signal:* a module outside `PROJECT.md`'s declared set, or several small modules that together do what one owner could. *Judge:* name the existing module that could own the capability and why it can't — "no owner fits" is a valid answer, "didn't check" is not. *Legitimate when:* it's genuinely deep and has no existing home. *Recommend:* merge/fold into the owner, or record the justification in `PROJECT.md`.
6. **Leaky abstraction / over-exposure** *(optional)* — *Signal:* a module exposing many internals; callers importing deep internal paths instead of the module's main-file API. *Judge:* should this surface be hidden behind the public interface? *Recommend:* narrow the interface, route callers through the main file.

## Anti-noise rules (non-negotiable)

The value of this audit is a short list of real findings, not a long list of suspicions.

- **A signal is a suspect, not a finding.** Confirm against the judgment rule and the source-of-truth docs before it enters the report.
- **Big ≠ bad, similar ≠ duplicate, off-the-list ≠ wrong.** Each dimension's "legitimate when" is a real exit — use it.
- **Never recommend a cure worse than the disease.** Weigh refactor cost and risk against the pain; a costly refactor for a cosmetic issue is itself a bad recommendation.
- **The map might be wrong, not the code.** When code contradicts a domain map, decide which is stale — a boundary finding can be "fix the map," which is an Orchestrator map-update (via `domain-map-workflow.md`), not a code refactor.

## Procedure

1. **Read the source of truth** — `AGENTS.md` (Architecture), `PROJECT.md` (modules, layers, soft limit), and the domain maps for the areas in scope. Read this workflow.
2. **Inventory** — list the modules/files in scope (the whole repo, or a scope the user named). Reconcile against `PROJECT.md`'s declared module set; note anything not on it.
3. **Gather candidates per dimension** — cheap signals only (file sizes, repeated patterns, cross-layer imports, thin files). Delegate bounded gathering to the **explorer** where it saves Orchestrator context; give it the dimension and specific questions.
4. **Confirm** — judge each candidate against its dimension rule and the source-of-truth docs. Drop the ones that hit a "legitimate when" exit. Survivors become findings.
5. **Prioritize** — rank by severity (how much it hurts maintainability, how many places it bites) weighed against how contained the fix is.
6. **Report** — present the findings (format below). No code is changed.
7. **Remediation handoff** — the Orchestrator turns accepted findings into planned refactor tasks (Step 4 onward), or appends deferred/systemic ones to `.vorch/FLAGGED.md`. A map-staleness finding is folded into the affected domain map instead.

## Output

```markdown
## Deep-Module Audit: <scope>

### Summary
[One paragraph: overall architectural health and the few things that matter most.]

### Findings
- **[severity] <dimension> — `file:line`** — [what's wrong and which principle it violates]. [The concrete recommended refactor.]

### Healthy (optional)
[A few places that are genuinely deep/clean, so the report isn't only negative and the bar is visible.]

### Deferred → FLAGGED.md
[Systemic or large-scope items recorded rather than actioned now.]
```

Severity: 🔴 (actively hurting, fix soon) / 🟡 (real, schedule it) / 🔵 (minor / opportunistic).

Keep the report short and true. A faithful "three real problems" beats a padded twenty.
