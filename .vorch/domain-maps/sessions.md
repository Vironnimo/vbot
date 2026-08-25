# Sessions

Persisted chat containers, session metadata, and current JSONL-backed storage.

## Overview

`core/sessions/` owns the system-managed Session domain. A Session belongs to exactly one Agent and stores canonical `ChatMessage` history under the agent's sessions directory: append-only UTF-8 JSONL for canonical messages plus three sidecars - a JSON metadata file, a dedicated completion-activity receipt, and an internal append-only continuation journal while visible work is active/interrupted. The domain owns persistence and file formats; Chat appends and loads through the session API, but no other domain constructs `.jsonl` paths directly. `session_search` uses recall backends for discovery; `session_read` uses `ChatSessionManager` for canonical records.

## Terms

Core terms (Session, Agent, Run) live in `.vorch/GLOSSARY.md`.

### Continuation journal
**Definition:** The internal append-only `<id>.continuation.jsonl` where Chat records interrupted-work recovery state while visible work is active. Sessions owns path/lifecycle; Chat owns record semantics.
**Not:** Canonical history - it never crosses into public history or Run events.

### Completion activity sidecar
**Definition:** The durable `<id>.activity.json` receipt holding the latest terminal Run and its exact read acknowledgement - isolated from general metadata so unrelated rewrites cannot lose a completion notification. Own lock, atomic writes.
**Not:** General Session metadata.

### Prompt-cache affinity id
**Definition:** The opaque per-Session lineage value Chat passes separately from Session identity; Provider adapters alone decide whether their wire can use it.
**Not:** A Session identifier. Compaction rotates it by design to break prior prompt prefixes.

## Storage contract

- Session files are append-only during normal operation; appends write one UTF-8 line through an append-only descriptor and fsync before returning. A read observing an invalid final line returns preceding valid messages; truncation+recovery happens only while the caller holds that Session's `write_lock` - unlocked readers leave the tail untouched because an append may be in flight. Complete invalid lines stay hard errors.
- History may include Tool messages with optional `timing`, interrupted Assistant messages (`interrupted: true` + normalized cause), and append-only `run_summary` annotations (run id, terminal status, timing, Run-owned `iteration_count`). Summaries annotate for reload/UI - never provider-visible fields; a summary without `iteration_count` means unknown (imported transcripts) and consumers must never reconstruct it from message records.
- Sidecar rewrites use the shared durable atomic-write helper (fsync temp, `os.replace`, directory fsync on POSIX). Compaction rotates `prompt_cache_affinity_id` after appending its checkpoint - breaking prior prompt prefixes by design.
- Session IDs: 1-128 ASCII letters/digits/hyphen/underscore, not starting with punctuation; public identifiers are UUIDs. Internal helpers validate before any path construction. Unknown future message fields may appear - validation goes through `ChatMessage.from_dict()`, never provider-specific shape assumptions.
- This boundary exists so SQLite can later replace JSONL without changing ChatLoop/RPC/channel/WebUI contracts; format changes use explicit converter scripts, never startup migrations.

## Interfaces

All Session async I/O runs through a dedicated eight-worker pool with write locks held across whole move/fork/archive operations; cancellation reports only after an in-flight filesystem mutation settles.

- `ChatSession(path)` is one session file handle: `append`/`append_many` (one fsynced batch), `load()` (validated, ordered), `load_since(cursor)` returning a batch plus opaque `{byte_offset, message_count, last_message_id}` cursor (a cursor whose anchor line no longer matches returns `None`, forcing disposable consumers to rebuild). Async counterparts run off the Event Loop and hold cancellation until started writes settle.
- Skill activation carriers: user triggers persist a `[skill-context] ` note via `activate_skill_context(name, data)` right after the triggering message; the Tool path deduplicates via `register_skill_activation(name, content) -> bool` (False only for identical name+content within the current Compaction epoch; the Result is the durable carrier). `activated_skill_contents(messages=None)` scans both carriers newest-wins after the latest committed checkpoint; module-level prefix/JSON parsers are the single parse shared by request building, statistics, and Compaction reconstruction - never re-implement them outside this domain. `project_tool_context_id`/`latest_project_tool_context_id` parse successful `project` Results selecting explicit Project Skill context.
- Notes: `add_note` persists kernel-internal notes; `begin_defer_notes`/`take_deferred_notes`/`flush_deferred_notes` bracket Tool dispatch so mid-turn notes join the ordered Result batch.
- `ChatSessionManager(data_dir)` is the path-free entry point (`create`/`get`/`get_or_create`/`exists`/`list`/`delete`/`archive`); callers never construct paths, and every method validates ids first. Every manager method takes trailing `project_id=None`: `None` -> global root, set -> the project anchor from the single layout helper imported from `core.projects` (no cycle). `write_lock` keys on the resolved transcript path, so identity and project sessions sharing an id get distinct locks. Rooted Identity Agents still pass `None` - internal working Project never changes storage addressing.
- **`write_lock(agent_id, session_id)`** is process-wide, context-reentrant, shared across manager instances. Reentrancy tracks a live lease through a `ContextVar`, extending to holder **and child tasks entering while the lease lives**: a Run holds the lock across its tool cycle and each tool runs in its own task, so a tool targeting its own session (e.g. `channel_send`) re-enters instead of self-deadlocking - keying on `current_task()` would deadlock there. A child outliving the original holder retains only an inactive copied lease and acquires normally. Protects short append/snapshot boundaries only; unbounded I/O runs outside it.
- `archive(agent_id, session_id, project_id=None) -> Path` is the deletion feature's storage step: moves transcript plus all sidecars under `archive/sessions/` mirroring the live tree (never colliding with agent/project archives), replacing existing archives, crash-safe via source lock + transcript-first replace. The server orchestrator holds `ChatRunManager.session_admission_guard` across archive so no new writer recreates the file mid-archive. `ChatSession.delete()` remains the raw primitive for tests/staleness only - the feature never hard-deletes.
- `fork(...)` copies into a fresh v4 UUID leaving the source untouched, omitting journal and activity sidecar (forks never inherit interrupted work or unread state); same-address forks inherit cache affinity, cross-boundary forks start fresh. Holds the source lock so the snapshot lands on a message boundary; destination always gains affinity + `fork_source` provenance keys; post-creation failure removes the destination before re-raising so failed forks never appear in `list()`.
- `move(...)` is storage-only relocation preserving the Session id: resets no current pointer and no recall index (callers own those); crash-safe ordering replaces the transcript first, then sidecars under their locks; Takeover additionally holds the admission guard over both addresses. Id preservation keeps checkpoints, cursors, unread state, and Terminals alive across moves.
- Strip-policy constants exported from `core.sessions`: fork always strips Channel binding/Sub-Agent linkage/reflection counters/run kinds (a fork is plain and unbound); cross-Agent forks and `/agent` moves additionally strip pinned catalog/seen-skills/cache-affinity so the new address derives fresh values. Constants are callers' shared policy values.
- Metadata: atomic arbitrary JSON read/write; `record_run_kind` idempotently accumulates producer categories (absent list = unclassified legacy, not background-only). Titles: manual `title` (trimmed single line, 200-char bound) over `auto_title` over legacy fallback; manual writes never overwrite automatic state; both setters publish through a callback seam for list invalidation. `SessionTitleService` derives a bounded local title then optionally replaces it via one background Model call receiving only a bounded projection of the first visible message - never history - accepting only a single final line of <=60 characters (reasoning/meta output rejected); Sub-Agent Sessions bypass it entirely (stable titles from parent delegation).
- Completions: `record_terminal_run` marks unread; `mark_terminal_run_read` acknowledges exactly the supplied id (never clearing newer ones) and notifies invalidation callbacks after releasing the activity lock; stale acknowledgements emit nothing; absent sidecar means clean pre-feature state. Runs whose activity policy is disabled are never recorded - Sessions does not infer policy from message internals.
- Projections: `list_with_metadata` gives summaries with timestamps derived via `bookend_timestamps()` (first/last-line fast path falling back to full load only when unusable); `list_completion_activity` reads existence plus completion projection only - for badges, not drawer decisions needing titles/timestamps/bindings.
- Cache lineage: address-derived deterministic value unless rotation is needed; `rotate_prompt_cache_affinity_id` persists a fresh epoch.
- `retarget_identity_agent_references(old, new)` touches only functional unqualified Sub-Agent parent links across live sidecars (historical `fork_source` and transcripts never change); snapshots support restore.
- Optional sidecar keys owned by other domains: complete `compaction_policy` override (absence inherits dynamically), `pinned_working_project_context` text written once by Chat before first request (Sessions persists, never renders/selects).

## Cross-Domain Rules

- Chat owns `ChatMessage`, request assembly, Run execution, note embedding, compaction behavior. Sessions owns persistence, metadata, validation, paths.
- Agents create sessions only through the manager; channel adapters use `exists()`/`get_or_create()`/metadata methods instead of deriving paths; server delegates keep storage details out of the public contract.
- Deletion archives, and its orchestrator first terminates Terminal Sessions sharing the exact `(project_id, agent_id, session_id)` owner - an archived chat must not leave interactive children ownerless.
- Recall/statistics indexes are disposable rebuildable projections; JSONL stays canonical (Statistics uses the Sessions-owned cursor).
- **Cross-accessor ordering:** a Run's tool cycle stays contiguous - out-of-band writers (channel observations, `channel_send`) take the `write_lock`; the Chat executor snapshots and appends initiating content under it as one ordered boundary; Compaction snapshots under the lock but Models outside it so out-of-band writers never wait on Provider latency.

## Constraints & Gotchas

- Skill contexts are ordinary prefixed notes rebuilt lazily per post-checkpoint epoch - use the exported predicate/parsers; never treat them as user-visible notes or re-implement parsing elsewhere. Pre-checkpoint carriers remain in history without conferring active instructions or env grants.
- Other internal note types follow the same prefix pattern: `[channel-message] `, `[skill-available] `, Chat-owned `[reply-surface] ` (stripped at reminder rendering against checkpoint positions). Interrupted readable Thinking belongs only to the continuation journal, never a note.
