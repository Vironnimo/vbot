# Core Shutdown And Session Cleanup Plan

## Goal

Fix the confirmed core cleanup bugs found during the bug hunt, then tighten shutdown tests around process, channel, cron, and log-viewer lifecycle behavior.

## Confirmed Bugs

1. `Runtime.stop()` calls `ProcessManager.stop()` and drops the manager reference, but `ProcessManager.stop()` only cancels the sweeper task. Active child processes keep running unmanaged.
   - Repro already run: after `manager.stop()`, a spawned Python sleep process had `returncode None` and status `running`.
   - Key files: `core/tools/process_manager.py`, `core/runtime/runtime.py`, `server/app.py`, `tests/core/tools/test_process_manager.py`, `tests/core/runtime/test_runtime.py`.
2. Session deletion removes only `<session>.jsonl`; `<session>.meta.json` survives and is reused if the same session id is recreated.
   - Repro already run: after delete, `sidecar_exists_after_delete True`; recreated metadata returned `{'is_subagent_session': True}`.
   - Key files: `core/sessions/sessions.py`, `tests/core/sessions/test_sessions.py`.
3. Shutdown cleanup is mostly synchronous while several services own asyncio tasks.
   - `ChannelService.stop()` cancels adapter/restart/stop tasks without an async await path.
   - `CronService.stop()` cancels job tasks without an async await path.
   - `server.app` can await async shutdown because FastAPI lifespan is async.
   - The broad warning-as-error xdist run produced a non-stable worker crash around log websocket shutdown; isolated tests passed, so treat it as a cleanup audit target rather than a standalone bug.

## Implementation Steps

- [x] Add `ProcessManager.aclose()` and make `ProcessManager.stop()` kill active sessions synchronously instead of only stopping the sweeper.
- [x] Add regression tests proving `ProcessManager.stop()` kills active sessions and `aclose()` awaits cleanup without leaving running child processes.
- [x] Add async close paths for `ChannelService` and `CronService` that call their sync `stop()` then await remaining tracked tasks.
- [x] Add `Runtime.aclose()` that stops channel, cron, and process services before closing logging and clearing references; keep `Runtime.stop()` as sync best-effort for non-async callers.
- [x] Update FastAPI lifespan shutdown to call `await runtime.aclose()` when available.
- [x] Update runtime/channel/cron/server tests for async shutdown behavior.
- [x] Delete session metadata sidecars together with session JSONL files and add regression tests for stale metadata not leaking into recreated sessions.
- [x] Run focused tests for process manager, runtime, channels, cron, sessions, and server app/websocket shutdown.
- [x] Run full backend quality gate.
- [x] Commit the completed fixes if all checks pass.

## Progress Notes

- Focused verification passed: `162 passed in 3.47s` for process manager, runtime, channels, cron, sessions, server app, and websocket tests.
- The combined focused run emitted the known Windows/Python 3.14 `_ProactorBasePipeTransport.__del__` unraisable warning once.
- Follow-up `-W error::pytest.PytestUnraisableExceptionWarning` localization passed for process/runtime, channel/cron/server, and sessions subsets, so no stable failing source was isolated.
- Broad warning-as-error verification passed: `1836 passed in 21.73s` for `tests/core`, `tests/server`, and `tests/storage`.
- Full quality gate passed: ruff format, ruff fix, ruff check, mypy, and pytest `2048/2048`.
- Committed as `7555d71 Fix core shutdown cleanup`.

## Design Notes

- Keep existing public sync `stop()` methods for compatibility, but add async `aclose()` methods where services own tasks that can be awaited.
- `ProcessManager.stop()` should be immediately protective: no active child process should keep running just because caller used the sync path.
- `ProcessManager.aclose()` should call `stop()`, then await the sweeper and all active session wait/reader tasks with `return_exceptions=True`.
- `Runtime.aclose()` should preserve the existing property behavior after shutdown: service properties raise `RuntimeError` because references are cleared.
- Logging should close after service shutdown, not before, so shutdown warnings/errors still have a live handler.
- Session sidecar cleanup should be idempotent: deleting an already-missing sidecar must not fail normal session deletion.

## Validation Commands

- `python -m pytest tests/core/tools/test_process_manager.py tests/core/runtime/test_runtime.py tests/core/channels/test_channels.py tests/core/automation/test_cron.py tests/core/sessions/test_sessions.py tests/server/test_app.py tests/server/test_websocket.py -q`
- `python -m pytest tests/core tests/server tests/storage -W error::pytest.PytestUnraisableExceptionWarning -q`
- `python scripts/quality.py`