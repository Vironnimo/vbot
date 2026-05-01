---
name: refactoring
description: Structured, focused refactoring with a clear goal, explicit scope, and no scope-creep. Trigger this skill when the task is to refactor, restructure, clean up, simplify, decouple, or reorganize existing code — in any phrasing. Examples: "refactor X", "clean up the auth module", "decouple Y from Z", "extract this into its own class", "this function is too long". Do NOT trigger for bug fixes or feature work.
---

# Refactoring

Refactoring means: **behavior stays the same, structure improves.** No new features, no bug fixes, no opportunistic improvements — only what directly serves the defined goal.

The most common refactoring failure is starting without a clear goal, then "improving" more and more along the way until nobody knows what actually changed — or until subtle behavioral assumptions get lost. This skill enforces focus and preserves correctness.

This skill provides the discipline rules. The plan, the file scopes, and the commits flow through the normal orchestrator-lite workflow (Planner writes the plan, Orchestrator commits).

---

## Prerequisites

Before any refactoring code change:

- **Tests must exist** for the affected code. If there are none — not even integration or E2E coverage — write them first as a Phase 0 in the plan. They are the safety net that proves behavior was preserved.
- **Behavior must be understood.** If the code's behavior isn't obvious, document the current behavior (in the plan, under "Hidden Constraints") before changing anything.

---

## Hidden Constraints — the biggest risk

The biggest refactoring risk isn't the changes you make — it's the assumptions you don't know you're breaking. Examples:

- A function retries exactly 3 times because the downstream API rate-limits on the 4th call
- An interface is called in a specific order because of an undocumented state machine
- A seemingly redundant null-check prevents a crash on Windows that doesn't reproduce on Mac
- A "slow" loop is intentionally throttled to avoid hammering a shared resource

These won't show up as test failures. They show up as production incidents weeks later. Before refactoring, ask: "Is there anything in this code that looks arbitrary but isn't?" If the answer is yes or unsure, it goes in the plan under **Hidden Constraints**.

---

## Plan requirements

When the Planner builds a refactoring plan, it must include:

- **Goal** — one sentence, with a single primary focus: Readability / Structure / Decoupling / Performance / Testability
- **Scope** — In and Out, both explicit. "Out" must name what is deliberately not touched.
- **Hidden Constraints** — the non-obvious behavior that must be preserved (see above)
- **Risks** — specific, e.g. "this module is called from 3 other places — interface changes break callers"
- **Done When** — verifiable criteria (tests pass + a structural criterion like "no function exceeds 30 lines", "Module A no longer imports from Module B")

---

## Allowed / Not allowed

**Allowed:**
- Renaming for clarity
- Extracting functions, classes, modules
- Moving code to its correct location
- Simplifying logic without behavior change
- Restructuring dependencies to reduce coupling

**Not allowed:**
- Adding features ("while I'm here…")
- Fixing bugs (note them, don't fix them)
- Changes outside the defined scope
- Swapping technologies unless explicitly requested

---

## Bugs and findings along the way

If you discover a bug or improvement opportunity during refactoring: **note it, don't touch it.** Add it to the Builder output under **Discoveries** (or **Architecture Concerns** if structural). The Orchestrator decides whether to spin up follow-up work.
