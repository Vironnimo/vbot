# Development: writing tests

How backend and WebUI tests are written so they stay deterministic and isolated: what to assert, how to wait, and what the shared test fixtures isolate. Read it before writing or changing a test. What to test at all: `AGENTS.md` -> Testing; running tests: `.vorch/PROJECT.md` -> Testing.

## Assertions and timing

- **Text assertions:** exact strings only for stable contracts (protocol tokens, persisted formats, accessibility names, forbidden internal values) or test-owned transport sentinels. For editable prose, errors and help, assert exception types, codes, structured fields, DOM roles or security invariants instead. Evaluate wording in scenarios, not substring tests.
- **Timing:** do not stabilize flaky tests by raising timeouts. Prefer controlled clocks and explicit synchronization. Rewrite or remove a test that needs five seconds or longer.
- **Draining tracked Tasks:** wait only for unfinished Tasks or remove each settled snapshot explicitly. Awaiting already-completed Tasks with `asyncio.gather` can return without yielding, so waiting for their scheduled discard callbacks in a loop can starve the Event Loop (Run cancellation cleanup: `runs/lifecycle.md` -> Cancellation).
- **Native process stand-ins:** stdlib-only Python children that model one native process use `getattr(sys, "_base_executable", None) or sys.executable`. On Windows a venv's `python.exe` adds a launcher process that retains pipe handles, changing process counts, direct-child relationships and stdout EOF; such tests must reach the interpreter directly. Children that need the locked environment's packages still use `sys.executable`.
- **Process termination assertions:** after stopping a real process tree, synchronize with each observed process's exit using a bounded `Process.wait` off the Event Loop. Windows Job termination and pipe EOF can precede the final process-handle signal; an immediate `is_running()` or zero-time wait can still report the terminating child as alive.

## Event Loop isolation

Async tests and fixtures on a worker share one session-scoped Event Loop, so a task a test leaves behind keeps running during later tests.

- Close every `Runtime` started inside a test's Event Loop with `await runtime.aclose()`. The root `tests/conftest.py` fails a test that leaves one started, its performance monitor running, or a canonical database it opened still open.
- A wait until the loop is quiet covers only the tasks started since the test began (compare with `asyncio.all_tasks()` taken at its start), never every task on the loop: earlier tests' tasks may run far longer (Swarm `settled`).
- Never patch the process-wide `asyncio.sleep`. A module whose waits tests skip exposes a module-local `_sleep` seam (for example `core.utils.retry._sleep`), and tests patch that seam. `tests/core/conftest.py` skips the `core.utils.retry` backoff waits in every core test, and `tests/core/chat/conftest.py` records the Chat recovery backoff (`core.chat.recovery._sleep`) instead of waiting; a test that observes those waits patches the seam itself.

## Process-wide caches

- **Native crash diagnosis:** `VBOT_TEST_CRASH_LOG_DIR` makes the root test hooks write faulthandler traces to one persistent file per Python process and restore that handler before each test. CI prints nonempty files immediately after the suite, before diagnostic retries; a dead xdist worker otherwise loses its captured traceback. Empty files are removed on clean shutdown.

- **Token estimation encoding:** production loads tiktoken's encoding on a background thread and counts with the character fallback until it is loaded (`providers/request-policy.md`), so a test that started with a cold encoding saw two counts for the same text when the load finished mid-test. The root `tests/conftest.py` loads it once per worker before the first test. A test that replaces or clears it restores the loaded one afterwards, as the fixture of `tests/core/utils/test_tokens.py` does; a test that needs the fallback patches `core.utils.tokens._load_estimation_encoding` for itself.

## Filesystem isolation

- **Home:** the root `tests/conftest.py` gives every test an empty home directory (`HOME` and `USERPROFILE`, `XDG_CONFIG_HOME` unset), so `Path.home()` and `~` never reach the real home, its `~/.vbot` data or its Git configuration. A value computed from the home at import time, such as `core.storage.storage.DEFAULT_DATA_DIR`, still names the real home: tests pass an explicit data directory.
- **Temporary files:** on Windows the root `tests/conftest.py` points `tempfile`, `tmp_path` and the `TMP`/`TEMP` of child processes at `<repo>-data/.tmp` beside the primary checkout, also from worktrees: outside the user profile, so the projects folder's antivirus exclusion covers test writes, and outside the repository, so test Git repositories and ignore rules stay their own. Assertions must not assume temporary paths are free of the word `vBot`.
- **Disk syncs:** the root `tests/conftest.py` skips the disk syncs that protect data only against power loss: for the whole session the database kernel opens every database with `synchronous=OFF` (the test seam `core.database.database.SYNCHRONOUS_OVERRIDE`) and `os.fsync` does nothing. A test that observes the production syncs (SQLite `synchronous` levels, `os.fsync` calls) carries the `durable` marker, which restores them for that test.
- **Unreadable files:** the `deny_access(path)` fixture of `tests/core/conftest.py` simulates an entry that exists but cannot be checked or read, the same way on Windows and Linux: listing a denied folder, and `stat` or a reading open of a path inside it (or of a denied file), raise `PermissionError`, while `os.path.exists`/`isdir`/`isfile` answer `False` as Python 3.14 does; writes still succeed, so a regression that takes the entry for missing overwrites it visibly. `monkeypatch.undo()` lifts the denial before the test inspects the result.
