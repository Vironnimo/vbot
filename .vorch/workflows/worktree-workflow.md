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
- Never stage or commit `.vorch/FLAGGED.md`; it is a local-only ignored runtime log.
- Subject: lowercase, no trailing period, max 72 chars.
- Body (optional): explain *why*, not *what*.
- Breaking change: append `!` → `feat(api)!: rename endpoint`.
- One logical unit = one commit (= one phase).
- **Never commit broken code.**

## Finalize

Run quality gates from within the worktree. Everything must be green — gates are the agent's responsibility; the merge tooling never runs them.

Write the Step 7 summary. Then merge yourself — no user confirmation is needed:

```bash
python scripts/worktree.py merge <task-name>
```

Use a generous shell timeout: the command blocks while other sessions' merges or repair windows finish, then merges the task branch into `main` (`--no-ff`), removes the worktree, its data dir, and the managed branch, and prints the merge commit.

## Conflicts: the protected repair window

When the merge reports conflicts, it rolls everything back — `main` stays exactly as it was. Resolve the overlap behind a protected window so nobody moves `main` while you fix:

1. Freeze `main` for your task (the window auto-expires after 15 minutes):

```bash
python scripts/worktree.py repair-start <task-name>
```

2. In your worktree, bring `main` into your branch (`git rebase main`), resolve the conflicts, commit, and rerun the quality gates.
3. Retry the merge — it runs inside your open window:

```bash
python scripts/worktree.py merge <task-name>
```

A successful merge closes the window automatically. If you give up instead, close it with `python scripts/worktree.py repair-finish <task-name>`. While a window is open, every other session's merge simply waits, so each conflict gets resolved exactly once against a frozen `main` — there is no loop of re-resolving the same overlap.

## Abandonment

If the task is cancelled or aborted, use the project-specific `delete` command (see `.vorch/PROJECT.md`) to clean up. Uncommitted changes in the worktree are discarded.

## Gotchas

- **One branch per worktree** — a branch can only be checked out in one worktree at a time. The `create` operation must create a fresh branch for the task.
- **Untracked files in worktree** — plan files (`.vorch/plans/`) and other untracked files exist only in the worktree; they are not visible in the main repo directory.
- **Commands are project-specific** — use the worktree command names and paths documented in `.vorch/PROJECT.md`; do not assume a fixed script path.
- **Never hand-merge into `main`** while sessions are running — all merges go through the merge command so the lock serializes them; a hand merge can collide with an automated one.
- **The merge runs no quality gates** — green gates before merging stay the agent's job, in the worktree.
- **Cleanup failure after a landed merge** — if the merge succeeded but worktree cleanup failed, the output says so; finish with the project-specific `delete` command manually.
