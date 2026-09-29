# Commit Check

`scripts/commit_check.py` runs as `.githooks/pre-commit` and `.githooks/pre-merge-commit`, and as the branch check of `scripts/worktree.py merge`. `.vorch/PROJECT.md` -> Testing says what it checks and when it blocks. This document explains when it runs tests, how it selects them and how it keeps its records. Read it before changing `scripts/commit_check.py`, `scripts/_test_impact.py`, `tests/file_dependencies.py` or the test isolation in `tests/conftest.py`, or when a selection surprises you. Tests: `tests/scripts/test_commit_check.py`, `tests/test_file_dependencies.py`.

## When tests run

- Tests run when work lands on `main`: on commits in the primary checkout and on merge commits there. Agents run the tests covering their change while they work; the hook repeats them once per landing instead of once per commit.
- A commit in a linked worktree gets the static checks only; its report says `tests NOT RUN in a worktree`. It leaves the worktree's records untouched, which only widens the next selection.
- `worktree.py merge` first runs `commit_check.py --branch` in the worktree, outside the merge lock, so other merges do not wait for it: the pytest tests the changes since the worktree's tested state affect. It uses the branch's own copy of the script, as the branch's commits use its own hook; a branch without one skips the check. A failure stops the merge before `main` changes.
- The merge commit then runs only the tests neither the branch check nor this checkout's runs cover as merged (see Merge commits), plus `vitest related`, the guard tests and the build for the merged WebUI changes, which have no records to reuse.

## Test selection

- The hook selects the tests in-process (`scripts/_test_impact.py`, ~1.5 s when Python changed): pytest-testmon's tests whose recorded executed code changed, the tests that failed in their last run, and the tests that read a data file changed since the tested state.
- The plugin `tests/file_dependencies.py` (a dependency of every test, so it holds only recording code) records per test the non-Python repository files it opened and the directories it listed.
- A change to `pyproject.toml` or to a file read while test modules are imported runs the complete suite, as do changed installed packages (testmon then drops its records).
- It starts one pytest run only when a test is selected: on the selected and the staged test modules, deselecting their recorded unaffected tests (an arguments file in the git directory), with testmon only recording (`--testmon-noselect -p no:TestmonSelect`; its selection plugin would select and order tests inside each xdist worker from records the controller rewrites meanwhile, and workers must collect alike), in one process or with as many workers as the recorded duration warrants. Starting pytest with all workers costs 7-10 s even when every test is deselected.
- testmon needs the C coverage tracer: `tests/conftest.py` sets `COVERAGE_CORE=ctrace`, because the default `sys.monitoring` core drops per-test dependencies. pytest-cov and testmon exclude each other.
- For changed WebUI and Extension page sources the hook runs `vitest related` plus the guard tests, then `npm run build`; it skips them with a notice without node or `webui/node_modules`.

## Records and tested state

- Both records are per checkout in the git-ignored `.testmondata` and `.testfiledeps`.
- The tested state is the tree of the index the checkout's last completed test step checked, plus the paths that had uncommitted work then; it is recorded with the records, and records without one describe HEAD. A rebase, a pull or a commit without the hook therefore only widens the next selection.
- `worktree.py create` copies the primary checkout's records into the new worktree, so they describe the state it forked from and the branch check selects only the branch's changes. A worktree without records (made without the script) copies them on its first branch check; a checkout without any runs the complete suite once (~10 min).
- The records attribute each failure: a failed test that depends on a changed file (staged, or changed on the branch for the branch check) blocks; one that depends on unstaged or untracked work and on no changed file is reported without blocking; any other failure is on committed code. That one runs once more, alone in one process: a test that fails only among the parallel runs of a busy machine passes then and is reported without blocking. A test that fails again blocks every commit until a separate commit fixes it.
- A lock in the checkout's git directory serializes its test runs.

## Merge commits

- A merge commit selects twice: against this checkout's records, and against the records of the worktree holding the merged branch (with the changes since that worktree's tested state, normally the changes made here since the fork). It runs only the tests both select; a test either side leaves out passed there with the code and files it has now.
- `git merge` runs the pre-merge-commit hook before it writes MERGE_HEAD, so the hook takes the merged head from `GIT_REFLOG_ACTION` (`merge <branch>`).
- The checkout adopts the branch's record of each test whose current state only the branch tested.

## Test environment

- Tests start without the repository-local git variables a hook exports (`git rev-parse --local-env-vars`, such as `GIT_DIR`), so their git calls in temporary directories cannot change the committing repository; `tests/conftest.py` drops them as well.
- The hook also drops `GIT_REFLOG_ACTION`, which a test's own `git merge` would keep.
