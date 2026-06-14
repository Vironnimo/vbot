# Worktree Workflow

Each task runs in a dedicated git worktree — an isolated directory with its own branch. Multiple tasks can run in parallel without touching each other or the main directory.

## Setup

Use the project's worktree commands to create a worktree for this task. Command names and locations are project-specific and are documented in `.vorch/PROJECT.md` (Development section).

The project-specific worktree tooling must provide these operations:
- `create` — create a new worktree and branch for a task
- `list` — show existing worktrees and their branches
- `delete` — remove a task worktree when the task is done or abandoned

Record the worktree path the `create` command outputs — **every agent you delegate to must work in this directory**, not the repo root.

Types: `feat` · `fix` · `docs` · `refactor` · `perf` · `test` · `chore`

## Key Rule: All Work Happens in the Worktree

Every agent you delegate to must be told the worktree path as their working directory. Do not delegate work against the main repo directory.

## Per-Phase Commit

After each phase completes, commit from **within the worktree directory**:

```bash
git add <files changed in this phase>
git commit -m "<type>(<scope>): <what this phase accomplished>"
```

**Commit rules:**
- Stage only application files — never the plan file.
- Subject: lowercase, no trailing period, max 72 chars.
- Body (optional): explain *why*, not *what*.
- Breaking change: append `!` → `feat(api)!: rename endpoint`.
- One logical unit = one commit (= one phase).
- **Never commit broken code.**

## Finalize

Run quality gates from within the worktree. Everything must be green.

Write the Step 7 summary. Then ask the user for merge confirmation — include the branch name and a one-line status.

On confirmation: from the **main repo directory** (not the worktree), merge:

```bash
git merge <branch> --no-ff -m "merge: <summary>"
```

Then clean up the worktree using the project-specific `delete` command (see `.vorch/PROJECT.md`).

## Abandonment

If the task is cancelled or aborted, use the project-specific `delete` command (see `.vorch/PROJECT.md`) to clean up. Uncommitted changes in the worktree are discarded.

## Gotchas

- **One branch per worktree** — a branch can only be checked out in one worktree at a time. The `create` operation must create a fresh branch for the task.
- **Untracked files in worktree** — plan files (`.vorch/plans/`) and other untracked files exist only in the worktree; they are not visible in the main repo directory.
- **Commands are project-specific** — use the worktree command names and paths documented in `.vorch/PROJECT.md`; do not assume a fixed script path.
