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
- `ChatSession.skill_context_messages()` — restores activated skill contexts as provider request messages.
- `ChatSession.delete()` — deletes the session file and its metadata sidecar (both `missing_ok`).
- `ChatSessionManager(data_dir)` — the path-free entry point for sessions: `create` / `get` / `get_or_create` / `exists` / `list` / `delete(agent_id, session_id)` resolve agent session roots so callers never construct `.jsonl` paths; all validate the session ID first.
- `ChatSessionManager.get_metadata(agent_id, session_id)` / `set_metadata(...)` — read/write arbitrary JSON-object metadata through the current sidecar file using atomic replace.
- `ChatSessionManager.list_with_metadata(agent_id)` — returns session summaries with `id`, `created_at`, `last_active_at`, plus sidecar fields.

## Current Storage Contract

- Session files use `.jsonl` and are append-only during normal chat operation.
- Session appends write one UTF-8 encoded line through an append-only file descriptor and fsync before returning. If a crash leaves an invalid final line without a trailing newline, `load()` treats that line as an incomplete write, truncates it, logs a warning, and returns the preceding valid messages. Complete invalid JSON/message lines remain hard errors.
- Session history may include `role: "tool"` messages with optional `timing` and `role: "run_summary"` annotations with `run_id`, terminal `status`, and `timing`. Run summaries are append-only records that annotate the preceding Assistant Run for reload/UI display; they are not provider-visible chat messages.
- Metadata sidecars use `<session-id>.meta.json` beside the session file.
- Session IDs must be 1-128 ASCII letters, digits, hyphen, or underscore and must not start with punctuation.
- Public/server-facing session identifiers are UUID strings. Internal helpers may accept custom IDs, but must validate them before any path construction.
- `list_with_metadata()` derives `created_at` and `last_active_at` from first and last persisted messages; empty sessions fall back to the session file mtime.
- Unknown future fields in persisted message JSON may appear; session storage validates through `ChatMessage.from_dict()` and should not depend on provider-specific metadata shape.

## Cross-Domain Rules

- `core/chat/` owns `ChatMessage`, provider-request assembly, Run execution, note embedding, and compaction behavior.
- `core/sessions/` owns session persistence, metadata persistence, ID validation, existence checks, and storage path details.
- `core/agents/` may create and validate current sessions only through `ChatSessionManager`.
- Channel adapters must use `ChatSessionManager.exists()` / `get_or_create()` / metadata methods instead of deriving `.jsonl` paths.
- Server RPC delegates should expose session operations through the runtime service and keep storage details out of the public contract.
- `core/recall/` may maintain derived search indexes, but JSONL remains canonical. Recall indexes must be disposable and rebuildable from `ChatSessionManager`.

## Constraints & Gotchas

- Skill contexts are not a separate store: they are persisted as ordinary `role: "note"` messages prefixed `[skill-context] ` and lazily rebuilt by scanning `load()` on first access (cached in-memory thereafter). Use the exported `is_skill_context_note()` predicate to recognize them — never treat these notes as normal user-visible or provider-injected notes.

## SQLite Migration Notes

- This domain boundary exists so a later SQLite implementation can replace JSONL without changing ChatLoop, server RPC, channel routing, or WebUI contracts.
- A SQLite implementation should preserve `ChatMessage.to_dict()` payload JSON as the canonical message shape, with indexed columns only for lookup/listing.
- Format changes must use explicit converter scripts under `scripts/converters/`; do not add silent startup migrations to app code.
