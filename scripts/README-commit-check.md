# Commit Check

`scripts/commit_check.py` runs as `.githooks/pre-commit` and `.githooks/pre-merge-commit`, and as the branch check of `scripts/worktree.py merge`. `.vorch/PROJECT.md` -> Testing says what it checks and when it blocks. This document explains when it runs tests, how it selects them and how it keeps its records. Read it before changing `scripts/commit_check.py`, `scripts/_test_impact.py`, `tests/file_dependencies.py` or the test isolation in `tests/conftest.py`, or when a selection surprises you. Tests: `tests/scripts/test_commit_check.py`, `tests/test_file_dependencies.py`.

## When tests run

- Tests run when work lands on `main`: on commits in the primary checkout and on merge commits there. Agents run the tests covering their change while they work; the hook repeats them once per landing instead of once per commit.
- A commit in a linked worktree gets the static checks only; its report says `tests NOT RUN in a worktree`. It leaves the worktree's records untouched, which only widens the next selection.
- `worktree.py merge` first runs `commit_check.py --branch` in the worktree, outside the merge lock, so other merges do not wait for it: the pytest tests the changes since the worktree's tested state affect. It uses the branch's own copy of the script, as the branch's commits use its own hook; a branch without one skips the check. A failure stops the merge before `main` changes.
- The merge commit then runs only the tests neither the branch check nor this checkout's runs cover as merged (see Merge commits), plus the WebUI checks for the merged WebUI changes, which have no records to reuse.

## Test selection

- The hook selects the tests in-process (`scripts/_test_impact.py`, ~1.5 s when Python changed): pytest-testmon's tests whose recorded executed code changed, the tests that failed in their last run, and the tests that read a data file changed since the tested state.
- The plugin `tests/file_dependencies.py` (a dependency of every test, so it holds only recording code) records per test the non-Python repository files it opened and the directories it listed.
- A changed file selects the tests that read it and the tests that listed its directory, since an added or removed file changes that listing. A directory no test listed may be new, and one that no longer exists was removed, which changes the listing of the directory above in turn: the tests that listed any directory up to the nearest existing one a test listed are selected too. So a new file in a new subfolder, such as a new bundled Skill or Extension, selects the tests that scan the folder above.
- A change to `pyproject.toml` or to a file read while test modules are imported runs the complete suite, as do changed installed packages (testmon then drops its records).
- It starts one pytest run only when a test is selected: on the selected and the staged test modules, deselecting their recorded unaffected tests (an arguments file in the git directory), with testmon only recording (`--testmon-noselect -p no:TestmonSelect`; its selection plugin would select and order tests inside each xdist worker from records the controller rewrites meanwhile, and workers must collect alike), in one process or with as many workers as the recorded duration warrants. Starting pytest with all workers costs 7-10 s even when every test is deselected.
- testmon needs the C coverage tracer: `tests/conftest.py` sets `COVERAGE_CORE=ctrace`, because the default `sys.monitoring` core drops per-test dependencies. pytest-cov and testmon exclude each other.
- For changed WebUI and Extension page sources the hook runs Prettier and ESLint on them, `vitest related` plus the guard tests, then `npm run build`. A change to any other file under `webui/` (package manifest and lock, `vite.config.js` with the Vitest configuration, `index.html`, `public/`) runs every Vitest test (`vitest run`) and the build; a change to the package manifest or lock, `eslint.config.js` or `prettier.config.js` runs `npm run format:check` and `npm run lint` over every source, as CI does.
- The WebUI checks run only on the locked packages: when `webui/node_modules/.package-lock.json` (npm's record of what it installed) differs from `webui/package-lock.json`, apart from optional packages for other platforms, every WebUI check blocks with the differing packages until `npm ci` in `webui/` installs the locked ones. The hook never installs packages itself: `node_modules` is shared by the checkout's sessions, `npm ci` needs the network, and on Windows it fails on files a running process holds. It skips the WebUI checks with a notice without node or `webui/node_modules`.

## Records and tested state

- Both records are per checkout in the git-ignored `.testmondata` and `.testfiledeps`.
- The tested state is the tree of the index the checkout's last completed test step checked, plus the paths that had uncommitted work then; it is recorded with the records. A rebase, a pull or a commit without the hook therefore only widens the next selection.
- `worktree.py create` copies the primary checkout's records into the new worktree, so they describe the state it forked from and the branch check selects only the branch's changes. A worktree without records (made without the script) copies them on its first branch check.
- Records that cannot answer select the complete suite (~10 min), whose run records afresh: no records, records SQLite cannot read (a missing `reads` table included), and records without a tested state, such as those of a plain `pytest --testmon` run, which may describe any state of the working tree. Before a test run the hook deletes a record that is no intact SQLite database, because testmon cannot open a corrupt `.testmondata`.
- The records attribute each failure: a failed test that depends on a changed file (staged, or changed on the branch for the branch check) belongs to the change; one that depends on unstaged or untracked work and on no changed file is reported without blocking; any other failure is on committed code. Every failure of the change or on committed code first runs once more, alone in one process: a test that fails only among the parallel runs of a busy machine passes then and is reported without blocking. A test of the change that fails again blocks the commit; one on committed code blocks every commit until a separate commit fixes it.
- A lock in the checkout's git directory serializes its test runs.

## Test core pool

- Every local pytest run, the hook's and an Agent's alike, claims its workers' cores from one pool per machine before xdist starts them (`tests/cpu_pool.py`, called first in `tests/conftest.py`'s `pytest_configure`): one lock file per physical core in `~/.cache/vbot-test-cores/`. Runs collect their cores one after another, so a run asking for many cores is not starved by smaller runs, and a run waits while the cores it asks for are busy. The operating system releases the locks of a process that dies.
- The hook sizes its runs to the pool (`_workers`, and the complete suite with all of it); `-n auto` means 2 workers locally. CI (`CI` set) and pytest runs that tests start inside a run holding cores (`VBOT_TEST_CORES_HELD`) skip the pool.
- Each run appends a JSON line to `runs.jsonl` there: checkout, kind (`commit`, `merge`, `branch`, `rerun` from the hook, `manual` otherwise), cores asked and granted, wait, duration, exit status and outcome counts, and for a complete suite the reason `_test_impact` gives.

## Merge commits

- A merge commit selects twice: against this checkout's records, and against the records of the worktree holding the merged branch (with the changes since that worktree's tested state, normally the changes made here since the fork). It runs only the tests both select; a test either side leaves out passed there with the code and files it has now.
- `worktree.py merge` stages the merge (`git merge --no-commit`) and commits it with `git commit`, whose pre-commit hook finds MERGE_HEAD. A hand-run `git merge` runs the pre-merge-commit hook before it writes MERGE_HEAD, so the hook takes the merged head from `GIT_REFLOG_ACTION` (`merge <branch>`).
- The checkout adopts the branch's record of each test whose current state only the branch tested.

## Test environment

- Tests start without the repository-local git variables a hook exports (`git rev-parse --local-env-vars`, such as `GIT_DIR`), so their git calls in temporary directories cannot change the committing repository; `tests/conftest.py` drops them as well.
- The hook also drops `GIT_REFLOG_ACTION`, which a test's own `git merge` would keep.
