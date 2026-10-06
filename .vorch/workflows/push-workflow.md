# Push Workflow

How to push `main` to GitHub. Commits and merges run static checks only, so `main` may hold a commit that breaks a test elsewhere. The push is where every such failure is found and fixed: `python scripts/push.py` checks the commit `main` points to completely and pushes exactly that commit only when every check passes. Nothing red reaches the remote.

A push is finished only when the `main-build` CI run it starts is green: CI also runs the Linux and Windows suites and builds the packages the user installs, and only a green run publishes them. Never use a plain `git push`.

## Steps

### 1. Run the push command

From the primary checkout:

```bash
python scripts/push.py
```

It checks the commit in a private checkout, not the working tree, so other sessions' uncommitted work does not affect the result: Ruff, mypy for Windows and Linux, the complete pytest suite, and the WebUI's format check, lint, Vitest and build. On Windows the complete pytest suite also runs on Linux in WSL (`linux pytest`), alongside the WebUI checks. E2E runs neither here nor in CI. Every step runs, whatever failed before it. A run takes several minutes, most of them the pytest suite, which first waits for the test cores other runs hold; use a shell timeout of at least 30 minutes. An interrupted run pushes nothing.

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

### 4. Watch CI to the end

A push to `main` that changes more than development documentation starts the `main-build` workflow (complete CI and the packages). Find its run and check that its `headSha` is the pushed commit:

```bash
gh run list --workflow=main-build.yml --branch=main --event=push --limit 1 --json databaseId,headSha,status
```

Follow it job by job, not only its end: check the jobs every minute or two (`gh run view <run-id> --json status,jobs`). As soon as one job fails, read that job's failures (`gh run view --job <job-id> --log-failed`) and start fixing while the other jobs still run; keep checking them, since they may add failures. Do not end your task before the run finished.

A red run is part of the push: fix every failure as in steps 2 and 3, whatever commit caused it, run `python scripts/push.py` again once the run has ended and every failure is fixed, and watch the new run. Repeat until a run is green. A run cancelled because a newer push superseded it says nothing; watch the newer one.

### 5. Report

Tell the user which commit was pushed, the green CI run, and what you fixed on the way: each failure, the commit that caused it, and the commit that fixed it. Name the flaky tests the report listed.

## Gotchas

- **Origin moved**: when origin's `main` has commits that local `main` lacks, the command pushes nothing, before the checks or when the push is refused after them. Tell the user and stop; do not bring those commits in on your own, and never force-push.
- **Only the checked commit is pushed**: commits that reach `main` while the command runs go with the next run; the report says when `main` moved on.
- **Checks only**: `python scripts/push.py --no-push` runs every check without pushing.
- **Preparation failures**: a failed `search engine` or `webui deps` step means the private checkout could not get its dependencies, usually through the network or npm; after a failed `webui deps` the WebUI checks did not run. Fix the cause and run again.
- **Linux tests**: `linux pytest` needs WSL with a distribution that has `python3` and its `venv` module; without one the step fails, it is never skipped. It runs `scripts/linux/push_tests.sh` on the checked commit, on the Python runtime the Linux packages bundle, like CI; it keeps uv, that runtime, the test environment, the search engine and the encodings in `~/.cache/vbot-push` inside WSL; the first run builds them and takes longer. A `linux tests:` line in its details names a setup failure. The last run's checkout stays there until the next run; reproduce a failure in it with `wsl.exe bash -c 'cd ~/.cache/vbot-push/checkout && TIKTOKEN_CACHE_DIR=../tiktoken ../venv-*/bin/python -m pytest <node id>'`.
- **Leftover checkouts**: a killed run can leave `.worktrees/.push-*` behind; the next push or `worktree.py` command removes it.
