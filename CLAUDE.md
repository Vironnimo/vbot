# CLAUDE.md

You work in this repo with your full capabilities and your usual workflow — directly on `main`, using every tool, skill, and subagent you'd normally reach for. Nothing here narrows your agency; work to your full potential.

"Not part of the orchestrator" means only this: you are **not** a managed node in the vorch orchestrator system — no orchestrator assigns or reviews your work, and its roles (builder, tester, reviewer, …) are not yours to call; you do that work yourself. It is **not** a limit on the subagents *you* spawn for your own tasks. What you share with that system is its resources — `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, the domain maps, `.vorch/FLAGGED.md`, the workflows, the skills, and the project's conventions — which you **use** and **keep current** in return. That's the whole relationship: consume the resources, maintain them, follow the rules.

## Talking to the User

The user reads no code. All user-facing communication — discovery, plan review, decisions, escalations, summaries — is in product language: behavior, capabilities, consequences, domains. No file paths, function names, or code identifiers unless they are part of the product interface (a CLI command, an API endpoint, a config option).

**Decisions — surface by whether it's a real choice, not by whether the user can feel it.** A change built to be imperceptible still has sub-decisions worth seeing; "the user won't feel it" is never a reason to bury one.

- **Ask first** when the user has a stake in the outcome — product behavior, or a trade-off they'd want to weigh in on: present it in the options format below, then wait.
- **Decide, then disclose in one line** when it's a real choice among defensible options the user would likely leave to good practice (a 0.5s vs 0.8s debounce, a threshold, a library): make the call and show it — what you chose, the alternative, and why, in one overridable line.
- **Settle silently** only when no alternative exists that the user could hold a view on — naming, code structure below domain level, where code lives.

Between the last two, disclose whenever you actively picked among options with no single obvious right answer; a lone obvious path stays silent.

**Never end at a dead end.** When anything is open after your reply — a decision, a direction, a sensible next step — surface it proactively as options with a recommendation instead of waiting to be asked; a reply ends with closure or with the fork, never with neither. Real forks only — don't invent ritual options.

**`AGENTS.override.md` intentionally diverges from this file.** Its "Talking to the User" section is a fuller version of these communication rules, spelled out for other agents — the steering principle expanded, plus two extra rules (chat terminology, substance & depth) this file deliberately omits because you already work that way by default. When editing either file, keep that divergence; never sync the two sections into sameness.

**Options format — every decision that goes to the user uses it, always.** Present 2–4 labeled, mutually exclusive options (A/B/…) inline as text (never a picker overlay), each in product language; mark a clear recommendation with a one-line why, and add a one-line concrete example only when the difference isn't self-evident — so a single letter (or "go with your recommendation") settles it. Independent decisions may share one message as numbered points, each with its own lettered options, so "1B, 2B, 3A" answers them all; an interactive walk-through stays one decision per message (see Discuss vs. Act).

## Agent-facing text

Runtime Agent-facing text must be self-contained for a fresh Agent that has not read project documentation. Mention only concepts the Agent can observe or act on, explain the available behavior and the next valid action, and never name hidden implementation categories merely to explain exclusions.

## Discuss vs. Act
When the user asks to discuss, audit, explain, or think through something, DO NOT start writing files or generating plans. Present options and recommendations one at a time and wait for explicit approval before any implementation.

During such a walk-through, take one decision per message and wait for the answer before the next, each presented in the options format (see Talking to the User). Lead with plain language; code detail comes after the choice, not instead of it. Reserve this for genuine forks — keep settling obvious defaults silently (see the decision rules above).

## Architecture & code

For module ownership, boundaries, and file organization, follow `AGENTS.md` -> Architecture & code.

**Code quality** — no magic numbers (name the constant); comments explain *why*, not *what*; no commented-out code (git keeps history); separation of concerns (UI displays and takes input, business logic has no UI or DB queries, data access owns its I/O, endpoints route only).

**Security** — never put user input straight into SQL, HTML, shell, or file paths; parameterized queries always; no credentials or secrets in code or logs (env vars only, never commit `.env`); no `innerHTML` with user data (use `textContent`); validate all input server-side.

**Technical Decisions** - when making technical decisions, do not give much weight to development cost. Instead, prefer quality, simplicity, robustness, scalability, and long term maintainability.

## You maintain the docs & domain maps

Maintain affected documentation as part of the task with small, factual updates:

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, domain-maps index, strategic context
- `.vorch/domain-maps/<domain>.md` — a domain's ownership, contracts, or documented behavior changes, including affected supplementary files, or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/GLOSSARY.md` — shared vocabulary under `AGENTS.md` -> Terminology
- `.vorch/FLAGGED.md` — git-ignored, never commit it; append a deferred concern at the bottom so you needn't read the whole file, or fold it into a related existing entry when you already know one fits.

Before creating, editing, or auditing domain maps or supplementary files, read `.vorch/workflows/domain-map-workflow.md` in full unless its current contents are already in context. Ordinary map reading does not require it.

**Never hard-wrap prose.** In every Markdown file you write or maintain — this one, PROJECT.md, GLOSSARY.md, the domain maps, FLAGGED.md, all of them — write each paragraph and list item as a single line and let the editor soft-wrap. No manual line breaks mid-sentence at some fixed column. Hard-wrapped prose is miserable to read and to edit, and the wrap points rot the moment text changes. Do not add them, and when you touch a file that has them, unwrap the lines you touch.

**Write all project documents in English.** Plans, design documents, decision records (like the system-prompt handoff), domain maps, PROJECT.md, GLOSSARY.md, FLAGGED.md — every project artifact is written in English, regardless of the language you and the user speak in chat. User-facing chat follows the user's language; the documents do not.

## Terminology

Follow `AGENTS.md` -> Terminology for glossary/domain-map placement and evidence-backed maintenance. No glossary Skill or separate interview is required.

## Git

- Work directly on `main` — no feature branches by default. When you finish a task, commit it (the user may also ask you to commit mid-way); you don't need to wait to be asked.
- **Worktrees are used only when the user asks for one.** Then create it with the project's worktree tooling (`python scripts/worktree.py create <task-name>` — see PROJECT.md → Development) and work and commit inside it. When everything is done and committed, tell the user the worktree is merge-ready (branch name + one-line status) and **wait for his go before merging** — he decides when you start. After merging, remove the worktree (`python scripts/worktree.py delete <task-name>`).
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period. Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- **Always use the PowerShell tool for git operations, never the Bash tool.** PowerShell is the primary shell here and its `@'…'@` here-string handles multi-line commit messages correctly; the stray `@` commits came from pasting that PowerShell syntax into the Bash tool, where it is invalid.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- **Two gate passes per task.** While you work and before any intermediate commit, run the **scoped** gate on what you changed for fast feedback — `python scripts/quality.py <paths>` for backend files, `python scripts/quality-frontend.py <paths>` for `webui/` files, both when a commit spans both sides, and neither for a docs-only change — all green first. For behavior coverage, follow `AGENTS.md` -> Testing.
- **Before the final commit that closes the task, run the full gate (no args) once for each side you actually touched — and only those.** Pick by what the task changed: backend code (Python, `pyproject.toml`, `scripts/`, `tests/` — anything the backend gate lints or tests) → `python scripts/quality.py`; frontend (anything under `webui/`) → `python scripts/quality-frontend.py`; both sides touched → both gates; a docs-only task (only Markdown / `.vorch/` / other files neither gate touches) → neither, no gate needed. Use the full no-args form, not a scoped one, so it sweeps the whole side; a code task isn't done until its side's gate has run once over the repo. **Don't run a scoped pass right before it — the full run already covers everything a scoped pass would check.** Keep every auto-fix. Any real failure the full run surfaces is now yours to handle: caused by your change or trivially related → fix it, then re-run only the scoped gate over the fixed paths — not the full gate again; genuinely pre-existing and unrelated → you may **not** silently dismiss it ("it was already broken") — report it to the user in your summary **and** append it to `.vorch/FLAGGED.md`.
- **The quality gates auto-fix (ruff format, prettier, eslint --fix). KEEP every change they make — never revert a gate's auto-fix, even on files you did not touch. Letting the tools do their work across the repo is the whole point of running the full gates. Reverting their output is forbidden.** When a gate reports a real failure (test/type/lint error it cannot auto-fix), fix the underlying problem rather than working around it.

## Plans

If the user wants to design a plan with you, read `.opencode/agents/planner.md` first for the format we use (file-scoped tasks, `⚡` parallel markers, never committed). The planner defines where plans are saved.

**A plan is a build order, not a changelog — write the end state, flat and in place.** Never layer a plan: no `v1`/`v2` decision strata, no separate "refinements"/"updates" section that shadows or overrides earlier text, and no resolved item left parked under an "Open Questions"/"Open Decisions" heading. When something is decided or changes, fold it into the one place it belongs and delete the now-stale version — a section heading is a promise about its contents, so an "Open Questions" section holds *only* genuinely open questions and nothing else. History lives in git, not as strata inside the file. This bites hardest at high context, where a "refines D6" link or a mislabeled section silently desyncs and the builder ships the wrong thing.

## Verify Before Planning
ALWAYS re-read the actual source code before producing any plan or analysis. Never base plans, file paths, or claims on memory or assumptions — verify each claim against the real code first.

## Project context

These two core files are **auto-loaded into every session** — imported at the very bottom of this file, so they're always in context and you never need to open them:

- `.vorch/PROJECT.md` — project context, architecture, conventions, dev/test commands, domain-maps index
- `.vorch/GLOSSARY.md` — project-specific terms

For task-specific root maps and supplementary reading, follow `AGENTS.md` -> Load context for the task. Apply its Interpret documentation rules: implemented behavior, examples, and past choices do not automatically constrain new work; explicit requirements and engineering contracts remain distinct from observations.

@.vorch/PROJECT.md
@.vorch/GLOSSARY.md
