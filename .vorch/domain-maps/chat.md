# Chat

Canonical provider-agnostic conversation representation and Agentic Loop execution in `core/chat/`.

## Overview

Chat owns canonical messages, provider-request shaping, Skill/Tool turn orchestration, and the loop that advances one admitted Run through Model and Tool steps. Run lifecycle, cancellation state, and Queue coordination live in `core/runs/`; Session persistence lives in `core/sessions/`; Compaction policy and checkpoint construction live in `core/compaction/`; Provider wire translation lives in `core/providers/`. Chat uses those public APIs and must not absorb their storage or lifecycle internals.

A Session is the persisted conversation container; a Run is one active execution inside it. Chat is the execution seam between them: it loads canonical Session history, resolves the Agent/Model/Tools, builds provider-ready context, persists canonical results, and emits provider-agnostic Run events.

## Data Model

- `ToolCall` (`core/chat/messages.py`) is the canonical Assistant-requested invocation: `id`, `name`, and JSON-object `arguments`.
- `ChatMessage` is the persisted canonical message. Every role carries `id`, UTC-offset `timestamp`, and `role`; constructors and `from_dict` enforce role-specific fields.
- Provider-visible conversation roles are `system`, `user`, `assistant`, and `tool`. Only `user` may carry `list[ContentBlock]`; Assistant messages may carry readable `reasoning`, opaque `reasoning_meta`, Usage, Tool Calls, and the internal `interrupted` marker.
- Kernel/history roles are `note`, `compaction_checkpoint`, `error`, `run_summary`, and `agent_takeover`. Their Provider/public visibility differs intentionally: notes are internal, checkpoints contribute through their Projection, errors are conditionally Model-visible, and run/takeover annotations never enter Provider requests.
- `timing` is `{started_at, completed_at, duration_ms}` with UTC display timestamps and a non-negative monotonic duration. Usage is a separate canonical object and never shares timing fields.
- `MessageSender` is valid only on user messages. `ReplySurface` is immutable per admitted interactive Run and represents WebUI/Desktop or one configured Channel.

Exact role fields, request rendering, Content Blocks, Continuation state, events, and Usage are task-gated in the References below.

## Interfaces and source ownership

- `core/chat/messages.py` owns `ChatMessage`, `ToolCall`, `MessageSender`, `ReplySurface`, validation/round-trip, effective Compaction history, note embedding, reasoning replay shaping, and dangling Tool-call repair.
- `core/chat/chat.py::ChatLoop` owns the Agentic Loop and exposes `send`, `start_run`, `queue_run`, `build_queue_update`, and manual Compaction. `ChatLoopDependencies` is Chat's explicit Runtime wiring contract; `run_executor` is the seam passed to `ChatRunManager`, and `child_loop` creates Sub-Agent loops with the same explicit collaborators.
- `core/chat/commands.py::CommandDispatcher` owns Built-in Commands end to end: catalog, recognition/parsing, scheduling policy, surface restrictions, domain guards, state changes, command-started Runs, and neutral outcomes. RPC and Channels supply addressing/`ReplySurface` and generically project feedback, navigation, Runs, and resource changes; command-specific behavior must not return to a transport. See `chat/commands.md`.
- `core/chat/model_resolution.py` owns Model-string parsing, exact Connection selection, per-Model Connection allowlists, and Run-local fallback resolution.
- `core/chat/continuation.py` owns the append-only Continuation journal, recovery fold, private reminder, and prompt budget. The journal never crosses the Chat boundary into public history or Run events. `core/chat/streaming.py` owns normalized delta accumulation, chunk timeout, and provider-agnostic stream-recovery decisions.
- `core/chat/tool_dispatch.py` owns effective Tool dispatch, Extension Tool hooks, Tool lifecycle emission, Skill activation, and `read_media` extraction. The Tool registry/result-envelope contracts live in `tools.md`; Extension dispatch semantics live in `extensions.md`.
- `core/chat/content_blocks.py`, `file_mentions.py`, and `block_resolver.py` own canonical content blocks, verified `@`-file snapshots, and last-mile media resolution. Blob storage and extraction live in `attachments.md`.
- `core/chat/usage.py` owns canonical whole-Session Usage aggregation. Provider adapters own wire normalization; the WebUI consumes the server projection rather than recalculating it.
- `core/chat/events.py` projects visible message/Tool/error events and strips opaque Provider data. Run sequencing and terminal lifecycle remain owned by `runs.md`.

## Invariants and conventions

- Chat remains Provider-agnostic. It may inspect declared adapter capabilities/policies but never Provider-specific wire payloads or raw SSE events.
- One active Run per `(project_id, agent_id, session_id)` and busy-Session FIFO semantics are Runs-domain invariants. Chat starts or enqueues through `ChatRunManager`; it does not maintain a second Queue.
- Each admitted executor captures immutable request inputs, including the optional two-field Sub-Agent Run override, then Chat resolves one Run-local execution context containing the Agent, Session, working Project inputs, primary Model target, Skill scope/catalog, Tool restriction, and Continuation state. Agentic progression, Tool dispatch, and automatic Compaction consume that context instead of the Runtime service surface or parallel parameter chains; a Model fallback replaces only the Model target.
- Each request is built from one canonical Session snapshot. The exact Tool definitions are known before System Prompt assembly, and the System Prompt is assembled per request rather than persisted as history. One narrow exception is input stability, not whole-prompt persistence: a Rooted Identity Agent's automatic Working Project Context block is rendered once into Session metadata and supplied verbatim on later builds; other System Prompt inputs keep their existing live lifecycles.
- Every Assistant Tool-call batch is followed by exactly one Tool Result per `tool_call_id`, in declared order, before any non-Tool request message. Concurrent execution must not change persistence/request order.
- `reasoning_meta` is opaque. Chat may preserve or strip it according to adapter policy and Model identity but never interprets or exposes it in public Run payloads.
- Notes are kernel-internal and excluded from ordinary history/UI. Any Model-visible rendering is an explicit Chat request-building rule; Provider adapters must never receive `role: "note"`.
- Session/address ownership (`run.project_id`) and the admitted working Project (`run.working_project_id`) are distinct. Working Project controls cwd/Project Files/Skills/prompt context; it does not move Identity Sessions, Workspace, Memory, permissions, or Sub-Agent addressing.
- Chat never infers or injects foreign Project Context from filesystem paths used by a Tool. An Identity Agent loads that context explicitly through the `project` Tool; the resulting ordinary Tool message persists in Session history without separate Chat metadata or reminder state. Chat reads the latest successful Project Tool Result to route Skill activation through that Project for the Identity Session, including later Runs, while reusing the unchanged Session-pinned Skill catalog (see `tools/project.md`).
- Visible output, canonical Assistant/Tool messages, and Run annotations persist at stable boundaries. Transient streaming deltas are never Session records.
- Timestamps are persisted as UTC ISO 8601 with an explicit offset; readers accept `+00:00` and `Z`. Public Session-id/path validation remains in `core/sessions/`.

## Constraints & Gotchas

- Public history must hide notes and opaque reasoning metadata while retaining visible errors, Compaction separators, run summaries, and Agent-takeover dividers according to the server contract.
- Model ids persisted in messages use user-facing `<provider>/<model-id>` form; adapters receive only their Provider-specific Model id. Connection/account pins are execution configuration, not part of the catalog key.
- The direct `ChatLoop.send()` compatibility path can still create a Session when none is supplied; server/product paths require explicit Session creation.
- Cancellation is best effort: it stops future progression and active processes but never rolls back already persisted history or output already delivered.
- Unknown future JSON fields and `error_kind` values may appear. Do not make Chat depend on Provider-specific metadata shapes or a closed error-kind vocabulary.
- Adapters opened for a Chat turn are closed when they expose `aclose`; OAuth token refresh and HTTP retry mechanics remain Provider-owned.

## References

Read these only when your task matches — not by default.

- Changing canonical request history, notes, Skill activation/catalog stability, reasoning replay, Content Blocks, file mentions, attachment routing, or Tool-cycle repair → `chat/request-building.md`
- Changing Run admission/execution, streaming recovery, cancellation, Continuation, Tool progression, Model/Connection resolution, fallback, or Compaction boundaries → `chat/run-execution.md`
- Changing token normalization, estimation, persistence, whole-Session aggregation, Usage events, or the server-owned token projection → `chat/usage.md`
- Changing Built-in Command recognition, scheduling, effects, neutral outcomes, surface availability, or projection → `chat/commands.md`
