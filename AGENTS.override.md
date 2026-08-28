# AGENTS.override.md

You work in this repo with your full capabilities and your usual workflow — directly on `main`, using every tool, skill, and subagent you'd normally reach for. Nothing here narrows your agency; work to your full potential.

"Not part of the orchestrator" means only this: you are **not** a managed node in the vorch orchestrator system — no orchestrator assigns or reviews your work, and its roles (builder, tester, reviewer, …) are not yours to call; you do that work yourself. It is **not** a limit on the subagents *you* spawn for your own tasks. What you share with that system is its resources — `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, the domain maps, `.vorch/DESIGN.md`, `.vorch/FLAGGED.md`, the workflows, the skills, and the project's conventions — which you **use** and **keep current** in return. That's the whole relationship: consume the resources, maintain them, follow the rules.

## Read at session start

Read these two core files completely before doing anything (even saying 'hi'), every session, no exceptions — there is no auto-import here, so loading them is on you:

- `.vorch/PROJECT.md` — project context, architecture, conventions, dev/test commands, domain-maps index
- `.vorch/GLOSSARY.md` — project-specific terms

They hold the project's rules and conventions — **follow them.** Read more as the task needs it: a domain's map under `.vorch/domain-maps/` (index in PROJECT.md) when you work that domain, plus any adjacent map your change touches; `.vorch/DESIGN.md` for UI work. Domain maps are first-pass orientation: use them to find the responsible domain, relevant contracts, likely source, and tests. They do not prove the current implementation and never replace source code, which remains the source of truth for implemented behavior.

## Chat terminology

Use vBot's established terms exactly as the glossary and domain maps define them. Say Tools, Run, Session, Queue, Skill, Provider — not translated alternatives.

## Agent-facing text review

Before creating runtime Agent-facing text in this repository, show the user its complete proposed wording verbatim with all new text in bold. Before changing existing runtime Agent-facing text, show exactly two complete versions: the current wording with every passage to be changed or removed in bold, then the proposed wording with every changed or added passage in bold. Do not substitute a summary or description of the text. Runtime Agent-facing text means text that vBot supplies to an Agent or Model as part of its runtime context or interaction, including System Prompt blocks, Tool descriptions, Skill instructions, handoffs, System Reminders, and orchestration prompts. It does not include repository governance or development documentation such as this file, domain maps, glossary entries, PROJECT.md, DESIGN.md, or code documentation.

## Architecture & code

**Few, deep modules** — small interfaces, implementation hidden inside. Module count is a budget; the system must stay small enough to hold in your head. Deep over wide: one module owning a capability end-to-end beats several shallow ones passing data around. Expose what callers need, hide the rest. **Default to extending an existing module — before adding a new module, file, layer, or abstraction, name the existing module that could own the capability and why it can't; "no existing owner fits" is a valid answer, "didn't look" is not, and an unjustified new module is a defect, not a style nit.** A module is too shallow when its interface is nearly as large as its implementation, when it's mostly pass-through, when it wraps something without adding an abstraction, or when callers must know its internals — fold it back or deepen it.

**Code quality** — no magic numbers (name the constant); comments explain *why*, not *what*; no commented-out code (git keeps history); separation of concerns (UI displays and takes input, business logic has no UI or DB queries, data access owns its I/O, endpoints route only).

**Security** — never put user input straight into SQL, HTML, shell, or file paths; parameterized queries always; no credentials or secrets in code or logs (env vars only, never commit `.env`); no `innerHTML` with user data (use `textContent`); validate all input server-side.

**Technical Decisions** - when making technical decisions, do not give much weight to development cost. Instead, prefer quality, simplicity, robustness, scalability, and long term maintainability.

## You maintain the docs & domain maps

There's no orchestrator here to keep these current — that's on you. When a change you make affects one, update it as part of the work (small, factual, not deferred):

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, domain-maps index, strategic context
- `.vorch/domain-maps/<domain>.md` — a domain's interface, boundary, invariant, or contract changes, or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/DESIGN.md` — design-system changes (colors, typography, spacing, components)
- `.vorch/GLOSSARY.md` — new or changed project-specific terms
- `.vorch/FLAGGED.md` — git-ignored, never commit it; append a deferred concern at the bottom so you needn't read the whole file, or fold it into a related existing entry when you already know one fits.

**⛔ HARD GATE — read the workflow only immediately before creating or editing a domain map.** Do not load `.vorch/workflows/domain-map-workflow.md` at session start, before reading or inspecting a domain map, during code analysis, while planning a possible change, or for a read-only domain-map audit. If and when you are about to create or edit anything under `.vorch/domain-maps/`, read the workflow in full immediately before the first write — every single time, including a one-line fix. If a read-only investigation later turns into an edit, that decision point is when you load the workflow.

**Never hard-wrap prose.** In every Markdown file you write or maintain — this one, PROJECT.md, GLOSSARY.md, the domain maps, FLAGGED.md, all of them — write each paragraph and list item as a single line and let the editor soft-wrap. No manual line breaks mid-sentence at some fixed column. Hard-wrapped prose is miserable to read and to edit, and the wrap points rot the moment text changes. Do not add them, and when you touch a file that has them, unwrap the lines you touch.

**Write all project documents in English.** Plans, design documents, decision records (like the system-prompt handoff), domain maps, PROJECT.md, GLOSSARY.md, FLAGGED.md — every project artifact is written in English, regardless of the language you and the user speak in chat. User-facing chat follows the user's language; the documents do not.

## Glossary

`.vorch/GLOSSARY.md` is read at every session start and is shared context for the whole project — keeping it right matters, which means keeping it **small**. Reading or understanding the existing glossary never requires the `glossary` skill. The glossary holds only core, cross-cutting terms every agent needs regardless of what it touches, plus terms the user says in conversation. A **domain-internal** term — one you only need once you are already working inside that one domain, and that the user never says — lives in a `## Terms` section inside that domain's map instead, never in the glossary. **One home per term, never both** (writing a domain-map term is domain-map work — the HARD GATE above applies). Watch for term candidates as you work and while discussing with the user:

- A term got **implicitly defined** through the conversation, a clarification, or a decision.
- A **project-specific term** in play could plausibly be misread (non-obvious meaning here).
- A term seems to cause **friction** because you and the user may mean different things by it.

Only project-specific terms — never standard programming terms or anything self-evident. When a term may matter, you may suggest adding or updating it and briefly explain why, but do not load the `glossary` skill and do not edit either term home yet. Load the skill only when the user explicitly asks for it or explicitly agrees after your suggestion. **Exception: when the current implementation makes an existing `.vorch/GLOSSARY.md` entry factually stale, correcting that entry is required project maintenance — load the `glossary` skill, update the entry, and commit it as part of the same work without seeking separate approval.** This exception does not authorize adding a term, choosing among ambiguous meanings, or making a judgment-heavy redefinition; those still require explicit user agreement. Once authorized, the skill handles triage (including which home), the interview, and writing the entry into the glossary or the domain map's `## Terms` section.

## Git

- Work directly on `main` — no feature branches by default. Commit each cohesive, verified set of changes without waiting for a separate request; a commit records repository state and says nothing about whether the user's broader objective is complete.
- **Worktrees are used only when the user asks for one.** Create them with the project's worktree tooling (`python scripts/worktree.py create <task-name>` — see PROJECT.md → Development), work and commit inside them, and report when their changes are merge-ready. Merge only after the user explicitly approves it, then remove the worktree with `python scripts/worktree.py delete <task-name>`.
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period. Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- Run the **scoped, non-mutating** gate on changed paths for fast feedback before committing code — `python scripts/quality.py --check <paths>` for backend files, `python scripts/quality-frontend.py --check <paths>` for `webui/` files, both when a commit spans both sides, and neither for a docs-only change. `--check` prevents formatter and linter edits, so a reported failure still refers to the code the Agent just read. Write tests together with the feature.
- Before recording a code change set as fully verified, run the full gate (no args) once for each side it touched — and only those. Backend code (Python, `pyproject.toml`, `scripts/`, `tests/` — anything the backend gate lints or tests) requires `python scripts/quality.py`; frontend code (anything under `webui/`) requires `python scripts/quality-frontend.py`; changes spanning both sides require both gates; docs-only changes (only Markdown / `.vorch/` / other files neither gate touches) require neither. Use the full no-args form so it sweeps the whole side. **Don't run a scoped pass right before it — the full run already covers everything a scoped pass would check.** Keep every auto-fix. Any real failure the full run surfaces is yours to handle: caused by the change or trivially related → fix it, then re-run only the scoped gate over the fixed paths — not the full gate again; genuinely pre-existing and unrelated → report it to the user and append it to `.vorch/FLAGGED.md`.
- **The quality gates auto-fix (ruff format, prettier, eslint --fix). KEEP every change they make — never revert a gate's auto-fix, even on files you did not touch. Letting the tools do their work across the repo is the whole point of running the full gates. Reverting their output is forbidden.** When a gate reports a real failure (test/type/lint error it cannot auto-fix), fix the underlying problem rather than working around it.
