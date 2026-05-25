# SubAgentCoordinator Refactor Plan

## Goal

Improve structure and testability by moving sub-agent orchestration out of the tool module into a dedicated coordinator while preserving current behavior.

## Scope

In:
- Add a `core/subagents/` domain module for orchestration and batch tracking.
- Move `SubAgentBatchTracker` and spawn/result orchestration helpers out of `core/tools/subagent.py`.
- Keep `core/tools/subagent.py` focused on tool names, schemas, display metadata, registration, and thin handlers.
- Update runtime wiring, tests, and specs for the new boundary.

Out:
- No durable parent-child storage yet.
- No WebUI timeline changes yet.
- No behavior changes to `blocking`, `queued`, `subagent_result`, depth limits, or per-turn limits.
- No prompt changes.

## Hidden Constraints To Preserve

- `subagent` keeps the same tool schema and result envelope shape.
- Non-blocking busy-session spawns return `status: "queued"` with `queue_item_id` instead of waiting for a `run_id`.
- Blocking spawns still wait for completion or timeout.
- `subagent_result` checks live Run state first, reports queued entries, then falls back to session history polling.
- `subagent_result` marks fetched before waiting on a live result to avoid the completion race.
- Parent cancellation removes queued child runs, cancels already-started child runs, and prevents stale batch-completion continuation triggers.
- Depth and per-turn limits keep their storage-backed defaults and atomic reservation semantics.
- The caller still cannot target its own active session.
- Batch completion still sends one internal parent trigger with a system-reminder note when all unfetched children finish.
- Tests currently reach some internals directly; move those internals deliberately rather than keeping legacy wrappers.

## Risks

- Runtime wiring was sensitive because `Runtime` previously stored `_subagent_batch_tracker` and registered tools directly from `core.tools.subagent`.
- Tests and specs import `SubAgentBatchTracker` from the tool module; all references must move cleanly.
- The coordinator still needs to start `ChatLoop` child runs without changing ChatLoop behavior.
- Moving too much at once could turn a structural refactor into a behavior change.

## Steps

1. Done - Create `core/subagents/` with coordinator and batch tracker ownership.
2. Done - Move orchestration helpers from `core/tools/subagent.py` into the new module.
3. Done - Keep `core/tools/subagent.py` as a thin registration layer that calls the coordinator.
4. Done - Update `Runtime` to create and hold a `SubAgentCoordinator` instead of a raw tracker.
5. Done - Update tests to import coordinator/tracker internals from `core.subagents` and keep registration on the tool wrapper.
6. Done - Update specs and project docs for the new domain boundary.
7. Done - Run focused subagent tests, then backend quality.

## Validation Progress

- Passed: `python -m pytest tests/core/tools/test_subagent.py tests/core/runtime/test_runtime.py::test_runtime_registers_subagent_tools`
- Passed: `python scripts/quality.py`

## Done When

- `core/tools/subagent.py` no longer owns run queueing, cancellation, batch tracking, or result polling logic.
- `core/subagents/` owns the coordinator and tracker.
- Public tool behavior is unchanged.
- Focused subagent tests pass.
- `python scripts/quality.py` passes.