# Runs Domain Extraction Plan

## Goal

Extract Run lifecycle and queue coordination into a dedicated domain while preserving behavior.

## Scope

In:
- Move Run primitives, event constants, queue item model, errors, and `ChatRunManager` from `core/chat/runs.py` to `core/runs/`.
- Update Chat, CommandDispatcher, Runtime, Automation, Channels, Tools, Server, and tests to import Run APIs from `core.runs`.
- Move the existing Run primitive tests to `tests/core/runs/`.
- Add/update domain documentation in `.vorch/specs/` and the project spec index.
- Run backend quality gates and a browser smoke test of the WebUI after the refactor.

Out:
- No behavior changes to cancellation, SSE replay, queue draining, Run event payloads, or WebUI queue rendering.
- No extraction of ChatLoop provider/tool execution.
- No persistence of queued Runs; queues remain in-memory.
- No split of server event bridging or WebUI queue state into new modules in this pass.

## Hidden Constraints

- Only one active Run may exist per `(agent_id, session_id)`, but different Sessions can run in parallel.
- Busy-session follow-up work is an in-memory FIFO queue and starts automatically after the active Run finishes.
- Queued internal Runs must stay hidden from public queue list responses.
- Cancellation is best effort: non-terminal late output is suppressed, terminal cancellation remains visible, and active host processes are cancelled through Run cancel callbacks.
- SSE replay uses monotonically increasing Run event sequence numbers, including transient delta events.
- WebSocket lifecycle summaries are derived from Run events but must not include SSE-only delta events.

## Risks

- Many modules import Run symbols through `core.chat`; removing that re-export requires coordinated updates.
- Server state/runtime fallback helpers use `isinstance(..., ChatRunManager)` checks, so all paths must reference the same class object after the move.
- Tests and channel/tool code also import direct event constants from the old `core.chat.runs` path.
- Live UI behavior depends on backend Run RPC/SSE/WS contracts even though the WebUI does not import backend code.

## Done When

- [x] `core/runs/` exists and exports the Run API.
- [x] `core/chat/` no longer owns or re-exports Run primitives.
- [x] Direct imports from `core.chat.runs` are gone.
- [x] Existing Run tests live under `tests/core/runs/` and pass.
- [x] `.vorch/specs/runs.md` exists and `.vorch/PROJECT.md` references it.
- [x] Relevant backend quality gates pass.
- [x] WebUI smoke test is completed in a browser.
- [x] Changes are committed.