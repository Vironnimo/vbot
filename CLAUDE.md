# CLAUDE.md

You are a solo agent working in this repo — your usual workflow, directly on `main`.

You are **not** part of the vorch orchestrator system. You **use** its shared resources —
`.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`, the domain maps, `.vorch/DESIGN.md`, `.vorch/FLAGGED.md`,
the workflows, the skills, and the project's conventions — and you **keep them current** in return.
That's the whole relationship: consume the resources, maintain them, follow the rules

## Talking to the User

The user reads no code. All user-facing communication — discovery, plan review, decisions,
escalations, summaries — is in product language: behavior, capabilities, consequences, domains. No
file paths, function names, or code identifiers unless they are part of the product interface (a CLI
command, an API endpoint, a config option).

**Decision filter:** Bring a decision to the user only if its outcome changes something the user can
see or feel in the product — framed as that consequence, with your recommendation and the
alternative. Everything else (code structure below domain level, patterns, naming, where code lives)
you settle inside the system and at most report in one sentence.

## Read at session start

Read both completely before doing anything, every session:

1. `.vorch/PROJECT.md` — project context, architecture, conventions, dev/test commands, domain-maps index
2. `.vorch/GLOSSARY.md` — project-specific terms

These hold the project's rules and conventions — **follow them.** This file deliberately does not
repeat them. Read more as the task needs it: a domain's map under `.vorch/domain-maps/` (index in
PROJECT.md) when you work that domain, plus any adjacent map your change touches; `.vorch/DESIGN.md`
for UI work; `.vorch/TESTER.md` for the live-testing playbook when you need to verify behavior in the
running app.

## Architecture & code

**Few, deep modules** — small interfaces, implementation hidden inside. Module count is a budget; the
system must stay small enough to hold in your head. Default to extending an existing module — a new
module, layer, or abstraction needs explicit justification. Deep over wide: one module owning a
capability end-to-end beats several shallow ones passing data around. Expose what callers need, hide
the rest.

**Code quality** — no magic numbers (name the constant); comments explain *why*, not *what*; no
commented-out code (git keeps history); separation of concerns (UI displays and takes input, business
logic has no UI or DB queries, data access owns its I/O, endpoints route only).

**Security** — never put user input straight into SQL, HTML, shell, or file paths; parameterized
queries always; no credentials or secrets in code or logs (env vars only, never commit `.env`); no
`innerHTML` with user data (use `textContent`); validate all input server-side.

## You maintain the docs & domain maps

There's no orchestrator here to keep these current — that's on you. When a change you make affects
one, update it as part of the work (small, factual, not deferred):

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, domain-maps index, strategic context
- `.vorch/domain-maps/<domain>.md` — a domain's interface, boundary, invariant, or contract changes,
  or a new domain emerges (a new domain also gets added to the domain-maps index in PROJECT.md)
- `.vorch/DESIGN.md` — design-system changes (colors, typography, spacing, components)
- `.vorch/GLOSSARY.md` — new or changed project-specific terms
- `.vorch/FLAGGED.md` — append deferred concerns (append-only, don't reorganize)

**Before you create, edit, or audit any domain map, read `.vorch/workflows/domain-map-workflow.md` first** — it
defines what belongs in a domain map (factual working notes, every claim backed by source/tests, no
exhaustive API/field dumps) and the rules for creating, maintaining, and indexing them.

## Glossary

`.vorch/GLOSSARY.md` is read at every session start and is shared context for the whole project —
keeping it right matters. Watch for glossary candidates as you work and while discussing with the user:

- A term got **implicitly defined** through the conversation, a clarification, or a decision.
- A **project-specific term** in play could plausibly be misread (non-obvious meaning here).
- A term seems to cause **friction** because you and the user may mean different things by it.

Only project-specific terms — never standard programming terms or anything self-evident. When a term
matters, propose handling it (add full definition / add placeholder / skip), then run the `glossary`
skill — it handles triage, the interview, and writing the entry into `.vorch/GLOSSARY.md`.

## Git

- Work directly on `main` — no feature branches by default. When you finish a task, commit it (the
  user may also ask you to commit mid-way); you don't need to wait to be asked.
- **Worktrees are used only when the user asks for one.** Then create it with the project's worktree
  tooling (`python scripts/worktree.py create <task-name>` — see PROJECT.md → Development) and work and
  commit inside it. When everything is done and committed, tell the user the worktree is merge-ready
  (branch name + one-line status) and **wait for his go before merging** — he decides when you start.
  After merging, remove the worktree (`python scripts/worktree.py delete <task-name>`).
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period.
  Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- Before committing, run the quality gates (PROJECT.md → Testing) for what you changed — all green
  first. Write tests together with the feature.

## Plans

If the user wants to design a plan with you, read `.opencode/agents/planner.md` first for the format
we use (file-scoped tasks, `⚡` parallel markers, never committed). The planner defines where plans are
saved.
