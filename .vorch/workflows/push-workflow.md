# Push Workflow

How to push `main` to GitHub. Commits and merges run static checks only, so `main` may hold a commit that breaks a test elsewhere. The push is where every such failure is found and fixed: `python scripts/push.py` checks the commit `main` points to completely and pushes exactly that commit only when every check passes. Nothing red reaches the remote.

## Steps

### 1. Run the push command

From the primary checkout:

```bash
python scripts/push.py
```

It checks the commit in a private checkout, not the working tree, so other sessions' uncommitted work does not affect the result: Ruff, mypy for Windows and Linux, the complete pytest suite, and the WebUI's format check, lint, Vitest and build. Every step runs, whatever failed before it. A run takes several minutes, most of them the pytest suite, which first waits for the test cores other runs hold; use a shell timeout of at least 30 minutes. An interrupted run pushes nothing.

Exit code 0 means the commit is pushed, or origin's `main` already had it. Continue with step 4.

### 2. Find the cause of every failure

The report marks each step PASS or FAIL, then shows each failure's details (failing tests, errors) and the path of the log with the complete output. Fix every failure, whatever commit caused it, not only those of your own work.

For each one, reproduce it directly (`python -m pytest <node id>`, `python -m mypy`, `cd webui && npx vitest run <file>`), then find the commit that caused it: `git log -- <path>` and `git blame` for the code and the test involved, `git bisect run` when the breaking commit is not obvious.

Tests the report lists as flaky failed under the parallel load and passed when run again alone. They do not block the push; name them in your report.

### 3. Fix, commit, run again

- Fix the code, not the test, unless the test is wrong: it asserts behavior that was changed on purpose, or fails by its own construction.
- When the intended behavior is unclear, ask the user instead of guessing.
- Commit each fix separately on `main` as `fix(<scope>): <what>`, once the tests covering it pass. A larger fix goes through a worktree like any other task.

Then run `python scripts/push.py` again. Repeat steps 2 and 3 until it pushes.

### 4. Report

Tell the user which commit was pushed and what you fixed on the way: each failure, the commit that caused it, and the commit that fixed it. Name the flaky tests the report listed.

## Gotchas

- **Origin moved**: when origin's `main` has commits that local `main` lacks, the command pushes nothing, before the checks or when the push is refused after them. Tell the user and stop; do not bring those commits in on your own, and never force-push.
- **Only the checked commit is pushed**: commits that reach `main` while the command runs go with the next run; the report says when `main` moved on.
- **Checks only**: `python scripts/push.py --no-push` runs every check without pushing.
- **Preparation failures**: a failed `search engine` or `webui deps` step means the private checkout could not get its dependencies, usually through the network or npm; after a failed `webui deps` the WebUI checks did not run. Fix the cause and run again.
- **Leftover checkouts**: a killed run can leave `.worktrees/.push-*` behind; the next push or `worktree.py` command removes it.
