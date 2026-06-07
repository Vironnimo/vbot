# CLAUDE.md

You work here as a normal solo agent — your usual workflow, directly on `main`.

`AGENTS.md` and `.opencode/agents/*` belong to a separate multi-agent orchestrator system. They do
**not** describe how you work — don't adopt their role constraints (plan execution, file-scopes,
"don't touch git", self-review). Ignore them except where this file sends you to one.

## Read at session start

Read both completely before doing anything, every session:

1. `.vorch/PROJECT.md` — project context, architecture, conventions, dev/test commands, specs index
2. `.vorch/GLOSSARY.md` — project-specific terms

These hold the project's rules and conventions — **follow them.** This file deliberately does not
repeat them. When you work on a domain, read its spec under `.vorch/specs/` (index in PROJECT.md),
and any adjacent spec your change touches. For UI work, also read `.vorch/DESIGN.md`.

## You maintain the docs & specs

There's no orchestrator here to keep these current — that's on you. When a change you make affects
one, update it as part of the work (small, factual, not deferred):

- `.vorch/PROJECT.md` — architecture, conventions, dev/test setup, specs index, strategic context
- `.vorch/specs/<domain>.md` — a domain's interface, boundary, invariant, or contract changes,
  or a new domain emerges (a new domain also gets added to the specs index in PROJECT.md)
- `.vorch/DESIGN.md` — design-system changes (colors, typography, spacing, components)
- `.vorch/GLOSSARY.md` — new or changed project-specific terms
- `.vorch/FLAGGED.md` — append deferred concerns (append-only, don't reorganize)

**Before you create, edit, or audit any spec, read `.vorch/workflows/spec-workflow.md` first** — it
defines what belongs in a spec (factual working notes, every claim backed by source/tests, no
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

- Work directly on `main`. No feature branches, no worktrees.
- When you finish a task, commit it — you don't need to wait to be asked (the user may also ask you
  to commit mid-way).
- Conventional format: `<type>(<scope>): <what>` — lowercase, ≤72 chars, no trailing period.
  Types: `feat` `fix` `docs` `refactor` `perf` `test` `chore`. Breaking change → `!`.
- One logical unit per commit; never batch unrelated changes; never commit broken code.
- Before committing, run the quality gates (PROJECT.md → Testing) for what you changed — all green
  first. Write tests together with the feature.

## Plans

If the user wants to design a plan with you, read `.opencode/agents/planner.md` first for the format
we use (file-scoped tasks, `⚡` parallel markers, saved under `docs/plans/`, never committed).
