# AGENTS.md

## Read at session start (if not already in your context/system prompt)

Read these two core files completely before doing anything (even saying 'hi'), every session, no exceptions — there is no auto-import here, so loading them is on you:

- `.vorch/PROJECT.md` — project context
- `.vorch/GLOSSARY.md` — project-specific terms

They hold the project's rules and conventions — **follow them.** Read more as the task needs it: a domain's map under `.vorch/domain-maps/` (index in PROJECT.md) when you work that domain, plus any adjacent map your change touches; `.vorch/DESIGN.md` for UI work. Domain maps are first-pass orientation: use them to find the responsible domain, relevant contracts, likely source, and tests. They do not prove the current implementation and never replace source code, which remains the source of truth for implemented behavior.

## Exploring rough ideas with the user

When the user brings an open-ended idea or asks what a feature should do, treat it as an early conversation whose direction is still being discovered.

- Start with a short, provisional high-level picture in a few sentences or at most three brief points. Leave room for the user to shape it; do not open with a comprehensive concept, feature catalog, architecture, or implementation plan.
- Then interview the user one important decision at a time. Give two or three concrete options with each question, mark your recommendation, and briefly explain why. Allow the user to suggest a different direction and wait for their answer before moving to the next decision.
- Start with the desired user experience, purpose, and scope. Introduce technical choices and edge cases only when the agreed direction makes them relevant.
- Keep each reply short and focused on the current decision. Build on previous answers instead of repeating the whole discussion or presenting a questionnaire upfront.
- Develop a detailed proposal or implementation plan only once the key decisions are settled and the user wants that next step. Exploration alone does not authorize implementation.
- Apply this workflow to exploratory discussion, not to clear implementation requests or explicit requests for a comprehensive analysis.

## Implementation discipline

**Requirements before code.** Before starting any implementation, state a concise list of concrete, later-verifiable requirements that the completed work must satisfy — each specific enough that you can check it after the fact with a test, a command, or an observable behavior. Keep it short; this is a checklist, not a design document.

**Verify against the list.** After implementation, go back through every requirement and report which ones pass and which fail, with the evidence (test result, command output, observed behavior). A requirement that can't be verified wasn't concrete enough — say so and tighten it. If you discover requirements you missed, add them and verify those too.

## Chat terminology

Use vBot's established terms exactly as the glossary and domain maps define them. Say Tools, Run, Session, Queue, Skill, Provider — not translated alternatives.

## Agent-facing text review

Before creating runtime Agent-facing text in this repository, show the user its complete proposed wording verbatim with all new text in bold. Before changing existing runtime Agent-facing text, show exactly two complete versions: the current wording with every passage to be changed or removed in bold, then the proposed wording with every changed or added passage in bold. Do not substitute a summary or description of the text. Runtime Agent-facing text means text that vBot supplies to an Agent or Model as part of its runtime context or interaction, including System Prompt blocks, Tool descriptions, Skill instructions, System Reminders. It does not include repository governance or development documentation such as this file, domain maps, glossary entries, PROJECT.md, DESIGN.md, or code documentation.

Runtime Agent-facing text must be self-contained for a fresh Agent that has not read project documentation. Mention only concepts the Agent can observe or act on, explain the available behavior and the next valid action, and never name hidden implementation categories merely to explain exclusions.

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
- `.vorch/domain-maps/<domain>.md` — a domain's interface, boundary, invariant, or contract changes, or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/DESIGN.md` — design-system changes (colors, typography, spacing, components)
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
- **Worktrees are the default for every task** — create one with the project's worktree tooling (`python scripts/worktree.py create <task-name>` — see PROJECT.md → Development) and work and commit inside it. When the quality gates are green and everything is committed, merge yourself: `python scripts/worktree.py merge <task-name>` lands the branch on `main`, removes the worktree, and serializes safely against other sessions' merges — on conflict it walks you through the protected repair window (see `.vorch/workflows/worktree-workflow.md`). No user confirmation is needed before merging.
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period. Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- **Two gate passes per task.** While you work and before any intermediate commit, run the **scoped, non-mutating** gate on what you changed for fast feedback — `python scripts/quality.py --check <paths>` for backend files, `python scripts/quality-frontend.py --check <paths>` for `webui/` files, both when a commit spans both sides, and neither for a docs-only change — all green first. `--check` prevents formatter and linter edits, so a reported failure still refers to the code the Agent just read.
- **Before the final commit that closes the task, run the full gate (no args) once for each side you actually touched — and only those.** Pick by what the task changed: backend code (Python, `pyproject.toml`, `scripts/`, `tests/` — anything the backend gate lints or tests) → `python scripts/quality.py`; frontend (anything under `webui/`) → `python scripts/quality-frontend.py`; both sides touched → both gates; a docs-only task (only Markdown / `.vorch/` / other files neither gate touches) → neither, no gate needed. Use the full no-args form, not a scoped one, so it sweeps the whole side; a code task isn't done until its side's gate has run once over the repo. **Don't run a scoped pass right before it — the full run already covers everything a scoped pass would check.** Keep every auto-fix. Any real failure the full run surfaces is now yours to handle: caused by your change or trivially related → fix it, then re-run only the scoped gate over the fixed paths — not the full gate again; genuinely pre-existing and unrelated → you may **not** silently dismiss it ("it was already broken") — report it to the user in your summary **and** append it to `.vorch/FLAGGED.md`.
- **The quality gates auto-fix (ruff format, prettier, eslint --fix). KEEP every change they make — never revert a gate's auto-fix, even on files you did not touch. Letting the tools do their work across the repo is the whole point of running the full gates. Reverting their output is forbidden.** When a gate reports a real failure (test/type/lint error it cannot auto-fix), fix the underlying problem rather than working around it.
