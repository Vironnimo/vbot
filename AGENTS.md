# AGENTS.md

## Read at session start (if not already in your context/system prompt)

Read these two core files completely before doing anything (even saying 'hi'), every session, no exceptions — there is no auto-import here, so loading them is on you:

- `.vorch/PROJECT.md` — project context
- `.vorch/GLOSSARY.md` — project-specific terms

They hold the project's rules and conventions — **follow them.** Read more as the task needs it: a domain's map under `.vorch/domain-maps/` (index in PROJECT.md) when you work that domain, plus any adjacent map your change touches. Domain maps are first-pass orientation: use them to find the responsible domain, relevant contracts, likely source, and tests. They do not prove the current implementation and never replace source code, which remains the source of truth for implemented behavior.

## Communication with the user

### Develop ideas together

When the user brings an idea or a rough plan, help develop it into a coherent approach. First understand the intended outcome and why it matters. Treat proposed implementation choices as starting hypotheses unless the user explicitly establishes them as constraints. Surface alternatives and explain when they would better serve the user's intent.

Start with a short, provisional picture of the whole undertaking. Identify its major areas, how they depend on each other, and the consequential questions that remain open. Take responsibility for surfacing important areas the user may not know to ask about.

Explore breadth before depth. Do not exhaust one subtopic while other areas still contain unresolved foundational questions. Choose the next question by its impact on the overall outcome, not simply because it follows from the previous answer. Introduce technical decisions when they materially shape feasibility, behavior, or later choices; defer incidental details.

Discuss one important decision at a time. Offer concrete options, explain their tradeoffs, and give a reasoned recommendation when useful. Keep replies focused and allow the user to challenge the framing or suggest another direction.

Periodically summarize what is settled, which foundational questions remain, and what can safely wait. Agreement with your recommendations does not establish that the important ground has been covered. Before proposing implementation, review the overall approach for missing capabilities, unresolved dependencies, and assumptions that could materially change it. Make remaining uncertainty visible.

Apply this approach to exploratory discussion. Respect explicit implementation requests and established decisions; exploration alone does not authorize implementation.

### Preserve the ongoing task across detours

Keep the active objective, earlier decisions, and unfinished commitments in view. A side task does not replace the main task unless the user changes direction.

After a detour, briefly state what was done and remind the user where the main task stands. Resume authorized implementation; for an ongoing discussion, name the open point and invite the user to continue there.

Before declaring completion, check the full agreed scope across the conversation, not just the latest subtask. Respect explicit pauses, cancellations, and changes of direction.

### State the expected outcome before implementation

Before implementation, briefly list what you understand the requested end result to be. Describe concrete, observable outcomes so the user can spot misunderstandings and you can carry the list through the work.

Keep the list proportional to the task. Base it on the user's request and established context; do not invent requirements, numerical targets, or additional scope.

Check the completed work against that list. In the final response, briefly state what was achieved, any material deviations or remaining gaps, and the relevant verification results. Keep this in the conversation; create a separate verification report only when requested.

### Present Agent-facing text changes

For runtime Agent-facing text, explain the meaningful changes in the conversation and show the relevant proposed wording verbatim. Include the corresponding current wording when comparison helps the user assess the change.

For larger changes, keep the conversation focused on the passages that materially affect Agent behavior. Make the complete changes available through the source-file diff. Do not create additional files solely to present the review. Show complete wording when the user requests it.

Runtime Agent-facing text includes System Prompt blocks, Tool descriptions, Skill instructions, and System Reminders supplied by vBot to an Agent or Model. Repository governance and development documentation are outside this review rule.

### Use established terminology

Use vBot's established terms as defined in the glossary and domain maps: Tools, Run, Session, Queue, Skill, Provider, and so on. Keep these terms untranslated.

## Agent-facing text quality

Runtime Agent-facing text must be self-contained for a fresh Agent that has not read project documentation. Mention only concepts the Agent can observe or act on, explain available behavior and valid next actions, and avoid hidden implementation categories.

## Architecture & code

**Few, deep modules** — small interfaces, implementation hidden inside. Module count is a budget: the system must stay small enough to hold in your head. Deep over wide: one module owning a capability end-to-end beats several shallow ones passing data around. Expose what callers need, hide the rest. **Default to extending an existing module — before adding a new module, file, layer, or abstraction, name the existing module that could own the capability and why it can't; "no existing owner fits" is a valid answer, "didn't look" is not, and an unjustified new module is a defect, not a style nit.** A module is too shallow when its interface is nearly as large as its implementation, when it's mostly pass-through, when it wraps something without adding an abstraction, or when callers must know its internals — fold it back or deepen it.

**Technical Decisions** — when making technical decisions, do not give much weight to development cost. Instead, prefer quality, simplicity, robustness, scalability, and long-term maintainability.

## Testing

Write tests **together with the feature** — never skip.

## Dependencies

If a task requires a new dependency, **check first** that no existing dependency already covers the need. Then install it, add it to `pyproject.toml` (or `webui/package.json` for frontend), and commit the lock file changes. Do not install packages speculatively — only what the current task requires.

## You maintain the docs & domain maps

There's no orchestrator here to keep these current — that's on you. When a change you make affects one, update it as part of the work (small, factual, not deferred):

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, domain-maps index, strategic context
- `.vorch/domain-maps/<domain>.md` — a domain's ownership, contracts, or documented behavior changes, including affected supplementary files, or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/GLOSSARY.md` — new or changed project-specific terms
- `.vorch/FLAGGED.md` — git-ignored, never commit it; append a deferred concern at the bottom so you needn't read the whole file, or fold it into a related existing entry when you already know one fits.

**⛔ HARD GATE — read the workflow before ANY domain-map work, no exceptions.** Before you create, edit, or audit *anything* under `.vorch/domain-maps/`, you MUST first read `.vorch/workflows/domain-map-workflow.md` in full. If you are about to write to a domain map or start a map audit and you have not read that workflow, stop and read it first — that read is the first action of the task, before any Edit, Write, or plan. It defines what belongs in a domain map (factual working notes, every claim backed by source/tests, no exhaustive API/field dumps) and the rules for creating, maintaining, and indexing them.

**Write all project documents in English.** Plans, design documents, decision records (like the system-prompt handoff), domain maps, PROJECT.md, GLOSSARY.md, FLAGGED.md — every project artifact is written in English, regardless of the language you and the user speak in chat. User-facing chat follows the user's language; the documents do not.

## Working with domain maps

When working on a domain, start with its root map at `.vorch/domain-maps/<domain>.md`; root maps are the always-read routing and safety layer. Treat the task's `read:` list as a starting point, not a ceiling, and read additional root maps when ownership or contracts cross domains.

After reading a root map, inspect its `## References` and load only the exact supplementary files whose trigger matches the current task. Never preload a domain's supplementary folder. A supplementary file adds task-specific depth, never replaces its root, and is deliberately absent from the Domain Maps index in `.vorch/PROJECT.md`.

Terminology has two homes: core, cross-cutting terms live in `.vorch/GLOSSARY.md`; a domain's own terms live in a `## Terms` section inside its map. So a domain map is also where that domain's vocabulary is defined — reading the map gives you both the domain and its words.

## Glossary

`.vorch/GLOSSARY.md` is shared context for the whole project — keeping it right matters, which means keeping it **small**. It holds only core, cross-cutting terms every agent needs regardless of what it touches, plus terms the user says in conversation. A **domain-internal** term — one you only need once you are already working inside that one domain, and that the user never says — lives in a `## Terms` section inside that domain's map instead, never in the glossary. **One home per term, never both** (writing a domain-map term is domain-map work — the HARD GATE above applies). Watch for term candidates as you work and while discussing with the user:

- A term got **implicitly defined** through the conversation, a clarification, or a decision.
- A **project-specific term** in play could plausibly be misread (non-obvious meaning here).
- A term seems to cause **friction** because you and the user may mean different things by it.

Only project-specific terms — never standard programming terms or anything self-evident. When a term matters, decide its home by the rule above, propose handling it (add full definition / add placeholder / skip), then run the `glossary` skill — it handles triage (including which home), the interview, and writing the entry into the glossary or the domain map's `## Terms` section.

## Git

- Work directly on `main` — no feature branches by default. When you finish a task, commit it (the user may also ask you to commit mid-way); you don't need to wait to be asked.
- **Small, quick, low-risk changes go directly on `main`; use a worktree for larger or uncertain tasks, or when concurrent work could interfere.** Create worktrees with `python scripts/worktree.py create <task-name>` (see PROJECT.md → Development), then work and commit inside them. Once the required quality gates pass and everything is committed, run `python scripts/worktree.py merge <task-name>` to merge into `main` and remove the worktree. For conflicts, follow `.vorch/workflows/worktree-workflow.md`. No user confirmation is needed before merging.
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period. Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- **Scoped gates are the default.** During work, use `python scripts/quality.py --check <paths>` or `python scripts/quality-frontend.py --check <paths>` when feedback is needed. Before each commit, run the applicable scoped gates without `--check` to apply fixes; add `--build` for frontend changes. Include all affected source and test areas, including callers whose behavior may change: automatic test mapping does not discover cross-domain dependencies. Docs-only changes need no gate.
- **Run the full gate (no paths) when effects are broad or cannot be reliably scoped**, including changes to shared infrastructure, dependencies, or configuration with widespread impact. Run only the affected side(s); a full gate replaces the scoped pre-commit run.
- **Keep every gate auto-fix and review the resulting diff.** Fix failures caused by the task or trivially related, then rerun the affected scope; rebuild if frontend changes affect the build. Report genuinely unrelated pre-existing failures and append them to `.vorch/FLAGGED.md`. Never revert auto-fixes or work around a real failure.
