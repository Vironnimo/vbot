# CLAUDE.md

You work in this repo with your full capabilities and your usual workflow — directly on `main`, using every tool, skill, and subagent you'd normally reach for. Nothing here narrows your agency; work to your full potential.

"Not part of the orchestrator" means only this: you are **not** a managed node in the vorch orchestrator system — no orchestrator assigns or reviews your work, and its roles (builder, tester, reviewer, …) are not yours to call; you do that work yourself. It is **not** a limit on the subagents *you* spawn for your own tasks. What you share with that system is its resources — `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, the domain maps, `.vorch/DESIGN.md`, `.vorch/FLAGGED.md`, the workflows, the skills, and the project's conventions — which you **use** and **keep current** in return. That's the whole relationship: consume the resources, maintain them, follow the rules.

## Talking to the User

The user reads no code. All user-facing communication — discovery, plan review, decisions, escalations, summaries — is in product language: behavior, capabilities, consequences, domains. No file paths, function names, or code identifiers unless they are part of the product interface (a CLI command, an API endpoint, a config option).

**Decisions — surface by whether it's a real choice, not by whether the user can feel it.** A change built to be imperceptible still has sub-decisions worth seeing; "the user won't feel it" is never a reason to bury one.

- **Ask first** when the user has a stake in the outcome — product behavior, or a trade-off they'd want to weigh in on: give your recommendation and the alternative, then wait.
- **Decide, then disclose in one line** when it's a real choice among defensible options the user would likely leave to good practice (a 0.5s vs 0.8s debounce, a threshold, a library): make the call and show it — what you chose, the alternative, and why, in one overridable line.
- **Settle silently** only when no alternative exists that the user could hold a view on — naming, code structure below domain level, where code lives.

Between the last two, disclose whenever you actively picked among options with no single obvious right answer; a lone obvious path stays silent.

## Discuss vs. Act
When the user asks to discuss, audit, explain, or think through something, DO NOT start writing files or generating plans. Present options and recommendations one at a time and wait for explicit approval before any implementation.

## Architecture & code

**Few, deep modules** — small interfaces, implementation hidden inside. Module count is a budget; the system must stay small enough to hold in your head. Default to extending an existing module — a new module, layer, or abstraction needs explicit justification. Deep over wide: one module owning a capability end-to-end beats several shallow ones passing data around. Expose what callers need, hide the rest.

**Code quality** — no magic numbers (name the constant); comments explain *why*, not *what*; no commented-out code (git keeps history); separation of concerns (UI displays and takes input, business logic has no UI or DB queries, data access owns its I/O, endpoints route only).

**Security** — never put user input straight into SQL, HTML, shell, or file paths; parameterized queries always; no credentials or secrets in code or logs (env vars only, never commit `.env`); no `innerHTML` with user data (use `textContent`); validate all input server-side.

## You maintain the docs & domain maps

There's no orchestrator here to keep these current — that's on you. When a change you make affects one, update it as part of the work (small, factual, not deferred):

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, domain-maps index, strategic context
- `.vorch/domain-maps/<domain>.md` — a domain's interface, boundary, invariant, or contract changes, or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/DESIGN.md` — design-system changes (colors, typography, spacing, components)
- `.vorch/GLOSSARY.md` — new or changed project-specific terms
- `.vorch/FLAGGED.md` — append deferred concerns (append-only, don't reorganize)

**Before you create, edit, or audit any domain map, read `.vorch/workflows/domain-map-workflow.md` first** — it defines what belongs in a domain map (factual working notes, every claim backed by source/tests, no exhaustive API/field dumps) and the rules for creating, maintaining, and indexing them.

**Never hard-wrap prose.** In every Markdown file you write or maintain — this one, PROJECT.md, GLOSSARY.md, the domain maps, FLAGGED.md, all of them — write each paragraph and list item as a single line and let the editor soft-wrap. No manual line breaks mid-sentence at some fixed column. Hard-wrapped prose is miserable to read and to edit, and the wrap points rot the moment text changes. Do not add them, and when you touch a file that has them, unwrap the lines you touch.

## Glossary

`.vorch/GLOSSARY.md` is read at every session start and is shared context for the whole project — keeping it right matters. Watch for glossary candidates as you work and while discussing with the user:

- A term got **implicitly defined** through the conversation, a clarification, or a decision.
- A **project-specific term** in play could plausibly be misread (non-obvious meaning here).
- A term seems to cause **friction** because you and the user may mean different things by it.

Only project-specific terms — never standard programming terms or anything self-evident. When a term matters, propose handling it (add full definition / add placeholder / skip), then run the `glossary` skill — it handles triage, the interview, and writing the entry into `.vorch/GLOSSARY.md`.

## Git

- Work directly on `main` — no feature branches by default. When you finish a task, commit it (the user may also ask you to commit mid-way); you don't need to wait to be asked.
- **Worktrees are used only when the user asks for one.** Then create it with the project's worktree tooling (`python scripts/worktree.py create <task-name>` — see PROJECT.md → Development) and work and commit inside it. When everything is done and committed, tell the user the worktree is merge-ready (branch name + one-line status) and **wait for his go before merging** — he decides when you start. After merging, remove the worktree (`python scripts/worktree.py delete <task-name>`).
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period. Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- **Two gate passes per task.** While you work and before any intermediate commit, run the **scoped** gates on what you changed (`python scripts/quality.py <paths>` / `python scripts/quality-frontend.py <paths>`) for fast feedback — all green first. Write tests together with the feature.
- **Before the final commit that closes the task, run BOTH full gates once with no args** (`python scripts/quality.py` **and** `python scripts/quality-frontend.py`) — always both, even when you touched only one side. A task isn't done until the gates have run once over the whole repo. Keep every auto-fix. Any real failure the full run surfaces is now yours to handle: caused by your change or trivially related → fix it; genuinely pre-existing and unrelated → you may **not** silently dismiss it ("it was already broken") — report it to the user in your summary **and** append it to `.vorch/FLAGGED.md`.
- **The quality gates auto-fix (ruff format, prettier, eslint --fix). KEEP every change they make — never revert a gate's auto-fix, even on files you did not touch. Letting the tools do their work across the repo is the whole point of running the full gates. Reverting their output is forbidden.** When a gate reports a real failure (test/type/lint error it cannot auto-fix), fix the underlying problem rather than working around it.

## Plans

If the user wants to design a plan with you, read `.opencode/agents/planner.md` first for the format we use (file-scoped tasks, `⚡` parallel markers, never committed). The planner defines where plans are saved.

## Verify Before Planning
ALWAYS re-read the actual source code before producing any plan or analysis. Never base plans, file paths, or claims on memory or assumptions — verify each claim against the real code first.

## Project context

These two core files are **auto-loaded into every session** — imported at the very bottom of this file, so they're always in context and you never need to open them:

- `.vorch/PROJECT.md` — project context, architecture, conventions, dev/test commands, domain-maps index
- `.vorch/GLOSSARY.md` — project-specific terms

They hold the project's rules and conventions — **follow them.** Read more as the task needs it: a domain's map under `.vorch/domain-maps/` (index in PROJECT.md) when you work that domain, plus any adjacent map your change touches; `.vorch/DESIGN.md` for UI work; `.vorch/TESTER.md` for the live-testing playbook when you need to verify behavior in the running app.

@.vorch/PROJECT.md
@.vorch/GLOSSARY.md
