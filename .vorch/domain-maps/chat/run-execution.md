# Chat Run Execution

Task-gated reference for Chat admission, provider/Tool progression, streaming recovery, interruption, Continuation, cancellation, and Run-local Model fallback. Read this when changing those behaviors; Run storage, queue state, and lifecycle types themselves remain owned by `runs.md`.

## Entry points and admission

`ChatLoop` exposes `send`, `start_run`, `queue_run`, `build_queue_update`, `continue_run`, `continuation_summary`, `discard_continuation`, and `compact_session`. `run_executor(content)` is the public seam handed to `ChatRunManager`; `child_loop(nesting_depth=...)` shares runtime services, attachment resolution, Compaction, reflection, and title notification for Sub-Agent execution. The server-facing paths require an existing Session; the legacy direct `send()` path may still create one when no `session_id` is supplied.

`start_run` persists visible content as a user message and internal content as a note. `sender`, `input_origin`, `reply_surface`, `project_id`, and dispatch-only `tool_restriction` ride the admitted executor. `queue_run` validates the same prerequisites and captures immutable execution data so a queued item applies surface and working-Project decisions only when it starts. `build_queue_update` returns replacement data without mutating Queue state.

Session/address ownership and execution context are separate. `run.project_id` is the public Session/address anchor and queue key; Identity Agent Runs keep it `None`. At admission Chat snapshots `run.working_project_id` from the addressed Config Agent's Project or an Identity Agent's explicit root. That snapshot supplies Tool cwd, Project Files/Skills, prompt context, file mentions, and Compaction rebuilds. If the Project/repository disappears before queued work starts, execution fails before new user content is persisted; Workspace is not a fallback.

## Agentic progression and events

The loop resolves the Agent, Model, exact Provider Connection, System Prompt, canonical history, allowed Tools, and attachment/request state before the first Provider call. Provider adapters yield normalized data only. Chat persists finalized Assistant/Tool/Error messages and emits provider-agnostic `RunEvent`s; opaque `reasoning_meta` never appears in visible events.

Streaming deltas (`assistant_output_delta`, `reasoning_delta`, `tool_call_delta`, `tool_call_stdout`, `tool_call_stderr`) are transient SSE events and never persisted. Stable events include Assistant output, Tool start/result, Compaction completion, Model fallback, error persistence, per-model-step Usage, and terminal Run lifecycle from the Runs domain. Tool failures use the stable Tool Result envelope with `ok=false`; there is no separate public `tool_call_failed` event.

Sibling Tool Calls from one Assistant turn execute concurrently. The next Model request waits for every sibling to terminate, and Chat persists Tool Results in the Assistant's original order. Dispatch goes only through the runtime Tool registry, effective allowlist, Extension decision pipeline, and optional Run-local restriction. Expected disallowed/unknown/failed calls become failure envelopes so the Model can recover.

Auto-compaction runs only at safe completed Model boundaries. Summary+Tail may compact after a complete Tool cycle or final Assistant response; Continuation waits for a final Assistant response so it does not split work whose next action is still unknown. Compaction mechanics and policy live in `compaction.md`.

## Model and Connection resolution

Agent Model strings use `<provider>/<model-id>[::<connection-local-id>[:<account-id>]]`. `core/chat/model_resolution.py` parses the optional pin. An explicit pin is used verbatim; an unpinned Model selects the first usable Provider Connection permitted by the Model's `connections` allowlist. A non-empty allowlist with no usable matching Connection fails instead of routing through a forbidden Connection. Unknown Model ids are left for the Provider API to reject after Provider/Connection validation.

If a retryable `ProviderError` escapes adapter retries and the Agent has a resolvable `fallback_model`, Chat may switch for the rest of the current Run. It emits `model_fallback_activated`, persists a System Reminder note, and leaves Agent configuration unchanged so the next Run starts from the primary Model. Fallback resolution uses the same pin and allowlist rules; no usable allowed Connection means the fallback is skipped. `NetworkError` is not a Model-fallback trigger.

## Continuation checkpoint

Every admitted visible Run owns a provider-neutral append-only Continuation journal in `core/chat/continuation.py`. It records original visible requests, readable reasoning/partial output, stable Assistant boundaries, Tool references/status, Compaction boundaries, and the interruption cause without copying opaque reasoning metadata or full Tool Results. Dirty streaming state flushes at most every two seconds; completed Model/Tool boundaries and interruption flush immediately. Canonical Session history remains authoritative for actual Tool Calls and Results.

An interrupted checkpoint attaches exactly once at executor start to the next visible Run: before a newly persisted user message or as the tail instruction for explicit Continue. A complete non-interrupted Assistant result resolves it; a further interruption extends the chain. Internal Runs neither consume nor resolve visible continuation state. Explicit Continue creates a new visible Run without a new user message and never replays an unknown Tool effect automatically. Discard removes the checkpoint while idle.

The public summary exposes only safe high-level state. Missing or unknown effects from `write`, `edit`, or `bash` instruct the Agent to inspect real filesystem/process state before repetition. Private readable reasoning and Tool detail remain internal.

## Streaming recovery

`core/chat/streaming.py::decide_stream_recovery` is the single provider-agnostic decision owner. It sees normalized errors plus whether a finish arrived, visible content was emitted, partial content exists, and a same-Model restart remains. It returns `ACCEPT_COMPLETE`, `RESTART`, `FALLBACK`, `PRESERVE_PARTIAL`, or `FAIL`; `ChatLoop._consume_stream_attempt` owns the side effects.

- After a normalized finish delta, a later transport/provider failure accepts the completed response; later Usage deltas already accumulated remain valid.
- Before visible output, unsupported streaming falls back once to non-streaming; retryable transient failures restart the stream while the two-restart budget remains; other failures propagate. Restart attempts persist no discarded partial state.
- After visible content, Chat never replays the request because that would duplicate output. It finalizes an `interrupted` Assistant message, drops any in-flight unexecuted Tool Call, preserves the partial answer, and leaves Continuation for the next visible Run. A reasoning-only interruption propagates while the checkpoint retains readable reasoning.
- Remote Providers use the per-chunk stall guard. Loopback, localhost/local-domain, RFC1918 private, and link-local base URLs are exempt so long local prefill does not look like a dead stream.

## Cancellation

Cancel is best effort. `Run.request_cancel` suppresses late non-terminal output, prevents new Tool progression, and calls the runtime ProcessManager cancellation scope. Already persisted history is never rolled back.

User cancel is not a recovery error. When visible streamed content already exists, Chat uses the same partial finalization path so text the user saw remains persisted; the Run still terminates `cancelled`. With no visible content no Assistant message is added, but the checkpoint may retain readable reasoning and the `run_summary` remains the timeline anchor. During Tool dispatch, computed sibling Results persist before cancellation is honored.

## Source and tests

- Loop/admission/fallback: `core/chat/chat.py`, `model_resolution.py`, `events.py`; tests in `test_chat_loop_lifecycle.py`, `test_chat_loop_fallback.py`, `test_chat_loop_model_resolution.py`, and `test_chat_loop_tools.py`.
- Continuation: `core/chat/continuation.py`; tests in `test_continuation.py` and `test_chat_loop_continuation.py`.
- Streaming and cancellation: `core/chat/streaming.py`, `chat.py`; tests in `test_streaming.py`, `test_chat_loop_stream_recovery.py`, and `test_chat_loop_streaming.py`.
