# Sessions

Persisted chat containers, session metadata, and current JSONL-backed storage.

## Overview

`core/sessions/` owns the system-managed Session domain. A Session belongs to exactly one Agent and stores canonical `ChatMessage` history under `<data_dir>/agents/<agent-id>/sessions/`. The current implementation remains append-only UTF-8 JSONL with one canonical message per line and a JSON metadata sidecar per session.

The Sessions domain owns persistence and file-format details. Chat code may append and load messages through the session API, but other domains must not construct `.jsonl` paths directly. Accessors and server delegates should use the runtime's `chat_sessions` service. Recall tools such as `session_search` use `core/recall/` backends, which in turn use `ChatSessionManager` for canonical data.

## Interfaces

- `ChatSession(path)` — handle for one current JSONL session file. The constructor validates the current `.jsonl` path shape.
- `ChatSession.create(sessions_dir, session_id=None)` — creates an empty session file in the supplied sessions directory. Custom IDs are validated before path construction.
- `ChatSession.id` — session identifier derived from the current filename stem.
- `ChatSession.sidecar_path` — current metadata sidecar path `<session-id>.meta.json`.
- `ChatSession.append(message)` — appends one compact UTF-8 JSON object plus newline.
- `ChatSession.load()` — returns validated `ChatMessage` objects in append order.
- `ChatSession.add_note(content)` — persists a kernel-internal `role: "note"` message and queues it for the next provider request.
- `ChatSession.begin_defer_notes()` / `flush_deferred_notes()` — bracket tool dispatch so notes created during a tool-use turn are persisted after that turn's tool-result messages.
- `ChatSession.drain_pending_notes()` — returns queued note messages and clears only the in-memory pending-note buffer.
- `ChatSession.activate_skill_context(name, data)` — stores one activated skill's `<skill_content>` context once per Session, persists it as an internal skill-context note, and returns a stable tool result envelope.
- `ChatSession.skill_context_messages(messages=None)` — restores activated skill contexts as provider request messages. Callers that already hold the session's loaded messages pass them to avoid a second full session read (the chat loop does this in `_build_request_messages`).
- `ChatSession.bookend_timestamps()` — returns `(first, last)` message timestamps by reading only the first and last complete JSONL lines (backward chunked tail read). Returns `None` when the fast path cannot answer (empty file, partial trailing write, unparseable bookend line); callers must then fall back to `load()`, which owns partial-write recovery.
- `ChatSession.delete()` — deletes the session file and its metadata sidecar (both `missing_ok`).
- `ChatSessionManager(data_dir)` — the path-free entry point for sessions: `create` / `get` / `get_or_create` / `exists` / `list` / `delete(agent_id, session_id)` resolve agent session roots so callers never construct `.jsonl` paths; all validate the session ID first.
- **Project scoping:** every manager method (`sessions_dir`, `create`, `get`, `get_or_create`, `exists`, `list`, `list_with_metadata`, `get_metadata`, `set_metadata`, `write_lock`, `delete`) takes a trailing optional `project_id=None`. `None` → the global root `agents/<agent-id>/sessions/` (unchanged behavior, every existing caller stays on this). A set `project_id` → the project anchor `projects/<project-id>/agents/<agent-id>/sessions/`. The anchor path comes from the single layout source `core.projects.project_sessions_dir(...)` (also used by `ProjectStore.sessions_dir`) — sessions imports that helper, not a `ProjectStore` instance, so there is no cycle. `write_lock` keys on the resolved transcript path, so a global and a project-scoped session sharing one id get distinct locks. The project binding is *carried in* by the caller (it lives in the Session meta, so an opener must already know `project_id` before it can find the file).
- `ChatSessionManager.get_metadata(agent_id, session_id)` / `set_metadata(...)` — read/write arbitrary JSON-object metadata through the current sidecar file using atomic replace.
- `ChatSessionManager.set_title(agent_id, session_id, title, project_id=None) -> str | None` — the single seam for naming a session: every titling path (the `session.rename` RPC, the `/rename` command, any later automatic titling) goes through it. Stores the user-facing display title under the `title` sidecar key; the title is collapsed to one trimmed line and capped at `SESSION_TITLE_MAX_LENGTH` (200, a safety bound — the UI ellipsizes). A blank result clears the key, so the session falls back to its automatic display label. Returns the stored title, or `None` when cleared. Touches only the sidecar (atomic replace via `set_metadata`, last-writer-wins), never the transcript, so it needs no `write_lock`.
- `ChatSessionManager.list_with_metadata(agent_id)` — returns session summaries with `id`, `created_at`, `last_active_at`, plus sidecar fields.
- `ChatSessionManager.write_lock(agent_id, session_id)` — returns the process-wide, task-reentrant async append lock for one session's transcript. Shared across manager instances (keyed by the resolved file path), so every writer reaches the same lock regardless of which manager it holds. Reentrant per task so a Run that holds it across its tool cycle can run a tool (e.g. `channel_send`) targeting its own session without self-deadlocking.
- `async ChatSessionManager.move(source_agent_id, session_id, target_agent_id, *, source_project_id=None, target_project_id=None, strip_meta_keys=frozenset())` — relocates a session's two files (transcript + sidecar) from one (agent, project) home to another and returns the destination `ChatSession`. Storage-only: it resets **no** "current" pointer and touches **no** derived recall index — the caller owns those (the `/agent` move orchestrator in `server/rpc/chat_methods.py`). `strip_meta_keys` is a parameter so the caller drops chat-owned keys (e.g. `visited_projects`) without sessions importing a chat constant. Crash-safe ordering: holds the source `write_lock`; fails cleanly (no partial move) if the destination transcript already exists; `os.replace` moves the transcript first (it alone defines the session to `list()`), then the key-stripped sidecar is written at the destination, then the source sidecar remnant is deleted (`missing_ok`). A crash between steps never loses the conversation — worst case is an orphan source sidecar, invisible to `list()`. The lock only guarantees append-contiguity; the real guard against a writer recreating the source file after the move is the caller's quiescence precondition (no active **or** queued run), not the lock.

## Current Storage Contract

- Session files use `.jsonl` and are append-only during normal chat operation.
- Session appends write one UTF-8 encoded line through an append-only file descriptor and fsync before returning. If a crash leaves an invalid final line without a trailing newline, `load()` treats that line as an incomplete write, truncates it, logs a warning, and returns the preceding valid messages. Complete invalid JSON/message lines remain hard errors.
- Session history may include `role: "tool"` messages with optional `timing` and `role: "run_summary"` annotations with `run_id`, terminal `status`, and `timing`. Run summaries are append-only records that annotate the preceding Assistant Run for reload/UI display; they are not provider-visible chat messages.
- Metadata sidecars use `<session-id>.meta.json` beside the session file.
- Session IDs must be 1-128 ASCII letters, digits, hyphen, or underscore and must not start with punctuation.
- Public/server-facing session identifiers are UUID strings. Internal helpers may accept custom IDs, but must validate them before any path construction.
- `list_with_metadata()` derives `created_at` and `last_active_at` from first and last persisted messages; empty sessions fall back to the session file mtime. It uses `bookend_timestamps()` so listing does not load full session files; the full `load()` path runs only when the fast path returns `None`.
- Unknown future fields in persisted message JSON may appear; session storage validates through `ChatMessage.from_dict()` and should not depend on provider-specific metadata shape.

## Cross-Domain Rules

- `core/chat/` owns `ChatMessage`, provider-request assembly, Run execution, note embedding, and compaction behavior.
- `core/sessions/` owns session persistence, metadata persistence, ID validation, existence checks, and storage path details.
- `core/agents/` may create and validate current sessions only through `ChatSessionManager`.
- Channel adapters must use `ChatSessionManager.exists()` / `get_or_create()` / metadata methods instead of deriving `.jsonl` paths.
- Server RPC delegates should expose session operations through the runtime service and keep storage details out of the public contract.
- `core/recall/` may maintain derived search indexes, but JSONL remains canonical. Recall indexes must be disposable and rebuildable from `ChatSessionManager`.
- **Cross-accessor append ordering:** a Run's tool cycle (the assistant tool-call message through its tool results) must stay contiguous in the transcript. Runs on one session are already serialized by `ChatRunManager`, but out-of-band writers are not — so every writer that can append while a Run might be active acquires `ChatSessionManager.write_lock(agent_id, session_id)` (`async with`). The Run holds it across its tool cycle; out-of-band note writers (channel observed notes, `session.link_channel`, `channel_send`) hold it just around their append, so they wait for an open tool cycle instead of splitting it. Any new out-of-band session writer MUST follow this rule.

## Constraints & Gotchas

- Skill contexts are not a separate store: they are persisted as ordinary `role: "note"` messages prefixed `[skill-context] ` and lazily rebuilt by scanning `load()` on first access (cached in-memory thereafter). Use the exported `is_skill_context_note()` predicate to recognize them — never treat these notes as normal user-visible or provider-injected notes.

## SQLite Migration Notes

- This domain boundary exists so a later SQLite implementation can replace JSONL without changing ChatLoop, server RPC, channel routing, or WebUI contracts.
- A SQLite implementation should preserve `ChatMessage.to_dict()` payload JSON as the canonical message shape, with indexed columns only for lookup/listing.
- Format changes must use explicit converter scripts under `scripts/converters/`; do not add silent startup migrations to app code.
