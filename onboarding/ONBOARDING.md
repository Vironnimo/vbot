# Onboarding

How to drop Orchestrator Lite into a project and start using it.

## 1. Place the files

```
<your-project>/
  AGENTS.md           ← copy to project root (community convention)
  .vorch/
    README.md         ← copy
    PROJECT.md        ← copy
    DESIGN.md         ← copy (only if UI work)
    agents/           ← copy
    skills/           ← copy
    onboarding/       ← copy (this folder)
```

`docs/plans/` is created automatically by the Planner during the first task.

## 2. Fill in `.vorch/PROJECT.md`

This is the single source of truth every agent reads first. Fill in the sections that apply, delete the ones that don't:

- **Architecture** — tech stack, module structure, data flow
- **Conventions** — naming, error handling, logging format, code style
- **Development** — env setup, build/run commands, package manager
- **Testing** — framework, file naming, how to run the suite
- **Context** — strategic notes (the Orchestrator maintains this as work progresses)

Keep entries short and factual. This is working notes for agents, not polished docs.

## 3. Fill in `.vorch/DESIGN.md` (only if UI work)

If the project has a frontend, fill in `.vorch/DESIGN.md`. Use `.vorch/onboarding/CREATE-DESIGN.md` as the format reference — it documents the YAML frontmatter schema (colors, typography, rounded, spacing, components) and the section structure (Overview, Colors, Typography, Layout, Elevation & Depth, Shapes, Components, Do's and Don'ts).

If there's no UI, delete `.vorch/DESIGN.md`.

## 4. Wire up the agents

Configure your IDE / agent runtime to discover the four agents in `.vorch/agents/` and skills in `.vorch/skills/`. The Orchestrator is the entry point — you only ever talk to it.

## 5. Use it

Prompt the Orchestrator with what you want done. It will:

1. Size the request (Nano / Small / Medium / Large)
2. Create a feature branch
3. Call the Planner (skipped for Nano)
4. Spawn Builders phase by phase
5. Call the Reviewer (skipped for Nano)
6. Merge into `main` and clean up

You don't need to direct individual agents — the Orchestrator delegates.

## What you control vs. what the system controls

| You | The system |
|---|---|
| Initial prompt with the request | Branching, commits, merges |
| Strategic context the Planner needs | Plan creation and file-scoping |
| `.vorch/PROJECT.md` / `.vorch/DESIGN.md` initial setup | `.vorch/PROJECT.md` / `.vorch/DESIGN.md` updates during execution |
| Approving installs (the Orchestrator asks) | Running tests, reviewing code |
| Manual recovery if a session crashes (read the plan file in `docs/plans/`) | Plan-file lifecycle (created by Planner, deleted on merge) |

## Skills

Skills auto-activate based on task type — you don't trigger them manually:

- `frontend-design` — UI implementation (Builder)
- `refactoring` — refactoring tasks (Planner / Builder)
- `debugging` — non-obvious bugs (Builder)

Adding more skills: drop a folder into `.vorch/skills/` with a `SKILL.md` containing YAML frontmatter (`name`, `description` with clear triggers).

## Manual recovery

If a session crashes mid-task, the plan file at `docs/plans/<plan>.md` shows what's done (`[x]` / `✅`) and what's left. Restart by telling the Orchestrator to resume from that file.

## When to use Lite vs. the full Orchestrator

See the table at the bottom of `.vorch/README.md`.
