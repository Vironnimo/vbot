# Worker Utilities

Cross-cutting home of `core/utils/workers.py`: the named worker pools and cancellation-safe settling helpers every domain uses to run blocking in-process work off the Event Loop and to finish admitted work before honoring cancellation.

## Worker pools

Blocking in-process work crosses named `BoundedWorkerPool` boundaries from `core/utils/workers.py`: each pool owns a dedicated executor, admits at most its worker count per Event Loop, and defers cancellation until an already-started mutation settles (cancellation while waiting for admission starts no worker), and records its admission wait, run duration and active/waiting gauges as `worker_pool.<name>.*` metrics (`performance/metric-catalog.md`). Pools are the stronger boundary than `asyncio.to_thread`, whose calls share the loop's default executor without application-level backpressure. Continuous Terminal I/O stays on its own executor so it cannot consume parser/prompt/Session/Tool capacity. The logging pipeline cannot use these pools: they measure through `core.performance`, which logs through it (`logging.md`).

An `OrderedWorker` from the same module is the single-thread variant for owners whose writes must land in submission order (full-document snapshots written by a scheduler and by operator edits, trace publication before cleanup): `call` blocks a synchronous caller, `call_async` awaits without blocking the loop, `hand_off` queues without waiting up to a caller-given limit, and `drain` waits for everything submitted; a submitted operation always runs, and a cancelled `call_async` caller waits until it has settled. It records only `worker_pool.<name>.run`.

## Settling on cancellation

`settle_before_cancelling(work, *, on_late_failure=None)` is that wait for any awaitable, such as an edit that saves and then updates memory: a cancelled caller waits for the work, then the cancellation wins. A failure of the work after that reaches no caller, so it is never dropped: `on_late_failure` reports it in the owner's words (RPC and Extension mutations, local speech shutdown), otherwise the helper logs it on `vbot.utils.workers` with the work's code name, an expected `VBotError` at WARNING and anything else at ERROR with its traceback (`tests/core/utils/test_workers.py`).

`finish_despite_cancel(work)` is its variant for work whose result must still reach the caller, such as a started Sub-Agent or a stored Live Tool call: the caller returns the result, and the cancellation (or an enclosing `asyncio.timeout`) is absorbed; a failure of the work propagates.

Owners that must finish admitted work before honoring cancellation use these rather than their own `shield` or `wait` loops (RPC mutation serialization in `server/rpc/_mutations.py`, Extension reload/disable/shutdown in `core/extensions/runtime.py`, local speech shutdown).

`cancel_and_wait(*tasks)` in the same utility owner cancels and drains owned background tasks: child cancellation is expected, while caller cancellation propagates after every child settles; child failures remain observable. Shutdown and stream cleanup use it instead of suppressing `CancelledError` around a direct task await, which also swallows cancellation of the caller. Performance shutdown settles its entire close sequence, including its watchdog and history workers, before propagating caller cancellation.

## Shared refreshes

`SharedRefresh(refresh, name=..., start_mark=None)` coalesces a refresh that rereads state others may change, such as a rescan of files: `run()` returns the outcome of a run that started after the call, so a caller sees every change made before it asked. Callers arriving before a run starts share it; callers arriving while one runs share the single follow-up that starts when it settles, so at most one run waits behind the running one however many callers come. `start_mark` is read when a run starts, and a caller passing an equal `current_mark` joins the running run instead (the Skill inventory passes the server event sequence). Callers wait with `settle_before_cancelling`, so a cancelled caller never cancels a shared run; finished runs are not kept; state is per Event Loop (`test_shared_refresh_*` in `tests/core/utils/test_workers.py`). Users: global Skill reloads (`runtime/hot-reload.md`) and `skill.inventory` (`skills/manager.md`).
