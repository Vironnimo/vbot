# SQLite FTS Recall Backend Plan

Status legend: `[ ]` not started, `[~]` in progress, `[x]` completed.

## Context

The memory MVP is mostly complete: pinned memory lives in `USER.md` and
`MEMORY.md`, and `session_search` now returns anchored windows and bookends over
current JSONL Sessions. SQLite FTS was intentionally left as follow-up work in
`docs/plans/memory-system-plan.md`.

This plan adds SQLite FTS without changing the core storage truth:

- `SessionStore` remains JSONL through `ChatSessionManager`.
- `RecallBackend` becomes the boundary used by `session_search`.
- `JsonlSessionRecallBackend` remains the default implementation.
- `SqliteFtsRecallBackend` is an optional first-party derived index.
- Future extension backends register through the same recall registry shape.

The design follows the Hermes research in
`stuff/researches/hermes-memory-system-research.md`: separate curated pinned
memory from searchable transcript recall, and treat SQLite FTS as an index
behind an interface rather than as the canonical Session store.

## Goals

- Keep `session_search` provider-visible schema and result contract stable.
- Extract a first-class recall boundary from the current `core/tools/session_search.py`
  implementation.
- Add an optional SQLite FTS5 backend over existing JSONL Sessions.
- Keep JSONL as source of truth; the SQLite DB is disposable and rebuildable.
- Make backend selection configurable through runtime settings.
- Prepare an official registration point so later extensions can provide recall
  backends without intercepting the `session_search` tool.
- Preserve current behavior when the backend is `jsonl_scan` or when SQLite FTS
  is unavailable, stale, or corrupt.

## Non-Goals

- Do not migrate Sessions from JSONL to SQLite.
- Do not change `ChatMessage.to_dict()` / `from_dict()` persistence shape.
- Do not add vector search or semantic embeddings.
- Do not combine pinned memory CRUD with Session recall.
- Do not add WebUI controls for recall backend selection in the first pass unless
  explicitly requested; raw `settings.json` support is enough for the backend
  work.
- Do not silently migrate old config. Unknown or invalid current config should
  follow the existing validation rules.

## Current State Read

Relevant current implementation details:

- `core/tools/session_search.py` owns argument parsing, JSONL scanning, message
  preview rendering, anchored views, bookends, and result envelopes in one file.
- `ChatSessionManager` owns Session path resolution and loading; recall code
  must keep using this API rather than constructing `.jsonl` paths directly.
- `ChatMessage` has stable `id`, UTC `timestamp`, `role`, `content`,
  `reasoning`, `tool_calls`, `tool_call_id`, `name`, `error_kind`, and
  `tail_boundary_id` fields.
- Skill-context notes are explicitly excluded from recall even when `note` is in
  the requested roles.
- `settings.json` raw validation currently warns for unknown top-level keys, but
  storage read paths reject schema errors.
- `settings.update` only supports explicit public sections. Recall backend
  selection does not need to be in the public Settings UI/RPC in the first
  implementation.
- Runtime registers `session_search` with `ChatSessionManager` directly today.
  This is the seam to replace with a recall backend.

## Target Architecture

```text
Runtime
  -> ChatSessionManager              # canonical JSONL Session source
  -> RecallBackendRegistry
      -> jsonl_scan                  # default, current behavior
      -> sqlite_fts                  # optional derived index
      -> extension-provided later
  -> selected RecallBackend
  -> session_search tool
      -> RecallBackend.browse/search/scroll

Pinned memory stays separate:

memory tool
  -> MemoryService
      -> PinnedMemoryBackend
          -> FilePinnedMemoryBackend # current default
          -> extension-provided later
```

Key boundaries:

| Boundary | Responsibility | Not Responsible For |
|---|---|---|
| `ChatSessionManager` | Canonical Session CRUD/load/list/metadata | Search ranking, FTS schema |
| `RecallBackend` | Browse/search/scroll read model for Session recall | Persisting canonical messages |
| `SqliteFtsRecallBackend` | Disposable index, FTS query execution, stale detection | Replacing JSONL Sessions |
| `session_search` tool | Provider schema, argument parsing, result envelopes | Backend-specific indexing |
| `MemoryService` | Pinned memory CRUD and backend selection for curated facts | Broad Session search |

## MemoryBackend Boundary

The SQLite FTS work should not make pinned memory disappear from the architecture.
It only means the immediate implementation target is `RecallBackend`, because FTS
indexes Sessions rather than curated facts.

Current pinned memory already has the beginning of the boundary:

```text
MemoryService
  -> FilePinnedMemoryBackend
      -> USER.md
      -> MEMORY.md
```

The follow-up architecture should make that boundary as explicit as recall:

```python
class PinnedMemoryBackend(Protocol):
    def list_entries(self, workspace: Path, scope: MemoryScope) -> list[MemoryEntry]: ...
    def add_entry(self, workspace: Path, scope: MemoryScope, content: str) -> MemoryEntry: ...
    def replace_entry(
        self,
        workspace: Path,
        scope: MemoryScope,
        entry_id: int,
        content: str,
    ) -> MemoryEntry: ...
    def remove_entry(self, workspace: Path, scope: MemoryScope, entry_id: int) -> MemoryEntry: ...
```

Add a parallel registry when extension backend registration is implemented:

```python
PinnedMemoryBackendFactory = Callable[[PinnedMemoryBackendContext], PinnedMemoryBackend]

class PinnedMemoryBackendRegistry:
    def register(self, name: str, factory: PinnedMemoryBackendFactory) -> None: ...
    def create(self, name: str, context: PinnedMemoryBackendContext) -> PinnedMemoryBackend: ...
```

Suggested settings shape for later:

```json
{
  "memory": {
    "backend": "file"
  },
  "recall": {
    "backend": "sqlite_fts"
  }
}
```

First-party memory backend names:

| Value | Meaning |
|---|---|
| `file` | Current `USER.md` / `MEMORY.md` backend |
| `sqlite` | Possible later pinned-memory DB backend, not part of this FTS pass |
| extension name | Provider/backend registered by an extension later |

For this FTS plan, do not implement a new memory storage backend. Do make the
docs and extension plan clear that `register_memory_backend(...)` is the sibling
of `register_recall_backend(...)`, and that `MemoryService` remains the only
tool-facing path for curated memory.

## Proposed Core Types

Add `core/recall/` with a main public module, likely `core/recall/recall.py`.

```python
class RecallBackend(Protocol):
    def browse(self, request: RecallRequest) -> RecallResult: ...
    def search(self, request: RecallRequest) -> RecallResult: ...
    def scroll(self, request: RecallRequest) -> RecallResult: ...


@dataclass(frozen=True)
class RecallRequest:
    agent_id: str
    session_id: str | None
    around_message_id: str | None
    query: str | None
    since: datetime | None
    until: datetime | None
    roles: tuple[str, ...]
    match_mode: Literal["all_terms", "any_term", "phrase"]
    limit: int
    context_messages: int
    bookend_messages: int
    sort: Literal["newest", "oldest"]


@dataclass(frozen=True)
class RecallBackendContext:
    data_dir: Path
    sessions: ChatSessionManager
```

Result model can start as JSON-compatible dictionaries to reduce churn:

```python
JsonObject = dict[str, Any]

class RecallBackend(Protocol):
    def browse(self, request: RecallRequest) -> JsonObject: ...
    def search(self, request: RecallRequest) -> JsonObject: ...
    def scroll(self, request: RecallRequest) -> JsonObject: ...
```

This keeps the first extraction mechanical. A later cleanup can introduce richer
result dataclasses if it pays for itself.

## Backend Registry

Add a small registry:

```python
RecallBackendFactory = Callable[[RecallBackendContext], RecallBackend]

class RecallBackendRegistry:
    def register(self, name: str, factory: RecallBackendFactory) -> None: ...
    def create(self, name: str, context: RecallBackendContext) -> RecallBackend: ...
    def names(self) -> list[str]: ...
```

Rules:

- Backend names use lowercase snake_case.
- Duplicate backend names are expected errors.
- Built-ins register first.
- Extension-provided registration comes later, after runtime has a stable
  registry object to pass into extension loading.
- Unknown configured backend falls back to `jsonl_scan` with a warning.

## Settings Shape

Raw `settings.json` shape:

```json
{
  "recall": {
    "backend": "jsonl_scan"
  }
}
```

Supported values at first:

| Value | Meaning |
|---|---|
| `jsonl_scan` | Default JSONL scan backend |
| `sqlite_fts` | Optional SQLite FTS index backend |

Implementation details:

- Add `recall` to raw settings validation known keys.
- Validate `settings.recall` as an object when present.
- Validate `settings.recall.backend` as one of known first-party values for raw
  validation. If extension backends need arbitrary names later, loosen this to a
  non-empty string and let runtime resolve against the registry.
- Add `StorageManager.load_recall_settings()` returning
  `{"backend": "jsonl_scan"}` by default.
- Do not add `settings.update` support in the first pass unless UI/API control
  is requested. This avoids new public Settings surface area before the backend
  proves itself.

## SQLite Index Location

Use a dedicated data-dir subfolder:

```text
<data_dir>/recall/session_index.sqlite
```

Add `recall` to `StorageManager.ensure_directories()`.

The file is a cache/index. It can be deleted at any time and rebuilt from JSONL
Sessions.

## SQLite Schema Sketch

Use stdlib `sqlite3`; no new dependency is needed.

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=1000;

CREATE TABLE IF NOT EXISTS indexed_sessions (
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  session_mtime_ns INTEGER NOT NULL,
  session_size_bytes INTEGER NOT NULL,
  indexed_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, session_id)
);

CREATE TABLE IF NOT EXISTS messages (
  row_id INTEGER PRIMARY KEY,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  message_index INTEGER NOT NULL,
  timestamp TEXT NOT NULL,
  role TEXT NOT NULL,
  search_text TEXT NOT NULL,
  UNIQUE (agent_id, session_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session
  ON messages(agent_id, session_id, message_index);

CREATE INDEX IF NOT EXISTS idx_messages_time
  ON messages(agent_id, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
USING fts5(
  search_text,
  content='messages',
  content_rowid='row_id',
  tokenize='unicode61'
);
```

Prefer explicit delete/reinsert for one Session during rebuild over triggers in
the first implementation. Triggers are useful later, but explicit writes are
easier to reason about while JSONL remains canonical.

If WAL fails on a filesystem, log a warning and continue with SQLite's default
journal mode. The index must remain optional, not a startup blocker.

## Indexed Search Text

Mirror the current JSONL scanner's searchable text so behavior stays familiar:

- textual content from user/assistant/tool/error/compaction messages;
- content block text for `TextBlock`;
- filename and media type for file/media blocks;
- assistant `reasoning`;
- tool `name`;
- error kind;
- assistant tool call names and JSON-serialized arguments.

Continue excluding skill-context notes from search and context windows.

Important: the index stores only derived search text and anchors. It does not
become the source for result windows. Windows/bookends are reloaded from
`ChatSessionManager` so the response reflects canonical JSONL.

## Query Semantics

Keep the current tool-level `match` values:

| Match | JSONL behavior | SQLite behavior |
|---|---|---|
| `phrase` | compacted substring contains phrase | FTS phrase query when possible; fallback to LIKE/JSONL if needed |
| `all_terms` | all compacted terms present | FTS `term AND term` |
| `any_term` | any compacted term present | FTS `term OR term` |

Sanitize FTS input:

- tokenize user query into safe terms for `all_terms` and `any_term`;
- quote phrase searches;
- reject or ignore empty terms after sanitization;
- never interpolate raw query strings into SQL;
- use parameters for all non-FTS values;
- for `MATCH`, build only from sanitized tokens.

FTS ranking can start with SQLite `bm25(messages_fts)` for query result order,
but the final ordering must respect the tool's `sort` semantics where visible:

- candidate Sessions still sort by `last_active_at` for browse.
- query matches can use FTS rank within candidate constraints, but preserve
  stable deterministic tie-breakers: timestamp, session id, message index.
- `sort="newest"` / `oldest"` should continue to mean activity/timestamp order
  if agents already rely on it. If FTS relevance changes that meaning, add an
  explicit later `rank` sort rather than changing existing semantics.

## Staleness And Rebuild

The first implementation should use lazy rebuild:

1. Build candidate Session summaries via `ChatSessionManager.list_with_metadata()`.
2. For each candidate Session that may be searched, compare current file stats
   with `indexed_sessions`.
3. If missing or stale, reindex that Session:
   - load messages through `ChatSessionManager.get(...).load()`;
   - delete prior rows for `(agent_id, session_id)`;
   - insert searchable messages and FTS rows in one transaction;
   - update `indexed_sessions`.
4. Execute FTS query after stale candidates are current.

For browse-only calls, SQLite is not needed; use the same Session metadata path
as JSONL.

For anchored scroll, SQLite is not needed unless future optimization warrants
it; load the canonical Session and find `around_message_id` as today.

Deletion handling:

- If a Session summary no longer exists, stale index rows may remain harmless.
- Add a lightweight cleanup during agent-level indexing: delete
  `indexed_sessions` and `messages` rows for sessions no longer returned by
  `ChatSessionManager.list_with_metadata(agent_id)`.

Corruption handling:

- Expected SQLite operational/index errors should log `warning`.
- Delete and rebuild the index once.
- If rebuild fails, return results from `JsonlSessionRecallBackend` for that
  call and include no backend-specific fields in the tool result.
- Unexpected programming errors should not be silently swallowed.

## Runtime Wiring

Runtime startup should become:

```text
ChatSessionManager(data_dir)
RecallBackendRegistry.with_builtins()
RecallBackendContext(data_dir, chat_sessions)
selected_backend = registry.create(storage.load_recall_settings()["backend"], context)
register_session_search_tool(tools, selected_backend)
```

Keep `Runtime.chat_sessions` as the public Sessions service.

Add a read-only runtime property only if useful:

```python
@property
def recall_backend(self) -> RecallBackend: ...
```

This is optional; tests can inspect tool behavior without exposing another
runtime property.

## Extension Registration Later

After first-party JSONL and SQLite backends are stable, extend extension loading:

```python
class HooksAPI:
    def register_recall_backend(
        self,
        name: str,
        factory: RecallBackendFactory,
    ) -> None: ...

    def register_memory_backend(
        self,
        name: str,
        factory: PinnedMemoryBackendFactory,
    ) -> None: ...
```

Ordering question:

- If extensions need to register backends before runtime selects one, runtime
  must create the recall and pinned-memory registries before
  `ExtensionRegistry.load(...)`, and `HooksAPI` must receive them.
- That changes the extension constructor surface, so do it as a separate commit.

Extension failures should follow current extension policy:

- registration/load failures log `error`;
- one bad extension backend should not prevent built-in `jsonl_scan` or `file`
  from working.

## Implementation Phases

### Phase 1: Extract Recall Boundary

- [ ] Add `core/recall/` with `RecallRequest`, `RecallBackend`,
  `RecallBackendContext`, and `RecallBackendRegistry`.
- [ ] Move current JSONL search/browse/scroll logic from `session_search.py`
  into `JsonlSessionRecallBackend`.
- [ ] Keep `session_search` argument parsing and provider schema in
  `core/tools/session_search.py`.
- [ ] Change `register_session_search_tool` to accept a `RecallBackend`.
- [ ] Keep result payloads byte-for-byte close where practical.
- [ ] Add focused tests for the JSONL backend plus existing tool tests.

Quality gate:

```bash
python scripts/quality.py core/recall core/tools/session_search.py tests/core/tools/test_session_search.py
```

### Phase 2: Add Backend Selection

- [ ] Add recall settings validation and storage normalization.
- [ ] Add built-in registry creation.
- [ ] Wire Runtime to select `jsonl_scan` by default.
- [ ] Add tests for default selection and invalid configured backend fallback.
- [ ] Update specs for runtime, settings, tools/session_search, and new recall
  domain.

Focused gate:

```bash
python scripts/quality.py core/recall core/runtime core/settings core/storage tests/core/runtime tests/core/settings tests/core/storage
```

### Phase 3: Add SQLite FTS Backend

- [ ] Add `SqliteFtsRecallBackend`.
- [ ] Create index DB under `<data_dir>/recall/session_index.sqlite`.
- [ ] Implement schema initialization, WAL attempt, and busy timeout.
- [ ] Implement per-Session stale detection using mtime and size.
- [ ] Implement Session reindex transaction.
- [ ] Implement FTS search and canonical JSONL window/bookend hydration.
- [ ] Implement corrupt-index rebuild and fallback to JSONL.
- [ ] Add unit tests with temporary data dirs and real SQLite.
- [ ] Add tests proving JSONL remains source of truth after index deletion.

Focused gate:

```bash
python scripts/quality.py core/recall tests/core/recall tests/core/tools/test_session_search.py
```

### Phase 4: Extension Registry Hook

- [ ] Add `HooksAPI.register_recall_backend`.
- [ ] Add `HooksAPI.register_memory_backend` for pinned-memory backends.
- [ ] Wire recall registry into extension loading.
- [ ] Wire pinned-memory registry into extension loading if memory backend
  selection is implemented in the same pass.
- [ ] Add tests for extension-provided backend registration.
- [ ] Document extension backend factory expectations for both backend types.

This phase can be deferred if first-party SQLite FTS is the immediate need.

### Phase 5: Full Verification And Commit

- [ ] Run full backend gate:

```bash
python scripts/quality.py
```

- [ ] Review diff enough to know what is being committed.
- [ ] Commit as one or more logical units. Suggested commits:
  - `refactor(recall): extract session search backend`
  - `feat(recall): select recall backend from settings`
  - `feat(recall): add sqlite fts backend`
  - `feat(extensions): register recall backends`

## Test Matrix

Core behavior:

- JSONL backend returns the same successful result shapes as current
  `session_search`.
- Unknown arguments and invalid combinations still return `invalid_arguments`.
- Missing Session / expected Session errors return `session_search_error`.
- Skill-context notes remain excluded.
- `roles=["note"]` still finds normal notes.
- `around_message_id` requires `session_id` and cannot combine with `query`.
- `bookend_start`, `window`, and `bookend_end` are hydrated from canonical JSONL.

SQLite behavior:

- First search builds the index lazily.
- Second search reuses a fresh index.
- Appending to JSONL marks the Session stale and reindexes before search.
- Deleting the SQLite file causes a rebuild.
- Corrupt SQLite file falls back to JSONL after one rebuild attempt.
- Phrase/all_terms/any_term produce expected matches.
- Role and time filters apply before or during FTS result hydration.
- Search text includes tool names, tool call arguments, reasoning, and content
  block text as the current scanner does.

Settings/runtime:

- Missing `settings.recall` defaults to `jsonl_scan`.
- `{"recall": {"backend": "sqlite_fts"}}` selects SQLite backend.
- Invalid raw settings shape is reported by config validation.
- Unknown backend logs warning and falls back to JSONL at runtime, unless raw
  validation chooses to reject it before runtime.
- Built-in `session_search` remains in the runtime tool list.

## Documentation Updates Required With Implementation

- `.vorch/PROJECT.md`
  - Add `core/recall/` to core modules.
  - Add `.vorch/specs/recall.md` to the Specs index.
  - Mention `<data_dir>/recall/` if the data-dir layout changes.
- `.vorch/specs/recall.md`
  - New domain spec for interfaces, registry, JSONL backend, SQLite backend,
    staleness, and fallback rules.
- `.vorch/specs/tools/session_search.md`
  - Change bound service from `ChatSessionManager` to `RecallBackend`.
  - Preserve public schema/result contract.
- `.vorch/specs/runtime.md`
  - Document recall registry/backend startup wiring.
- `.vorch/specs/settings.md`
  - Document raw `settings.recall.backend`.
- `.vorch/specs/sessions.md`
  - Clarify JSONL remains canonical while recall indexes are derived.
- `.vorch/specs/memory.md`
  - Keep the pinned-memory/recall split explicit.

## Risks And Decisions

| Topic | Recommendation |
|---|---|
| Message anchor | Use `ChatMessage.id`; keep `message_index` only as an index-local ordering helper. |
| Source of truth | JSONL remains canonical; SQLite may be deleted and rebuilt. |
| Startup cost | Do not eagerly index all Sessions at startup. Use lazy indexing. |
| FTS result order | Preserve existing sort semantics first; add explicit rank sort later if desired. |
| Public settings | Start with raw config only; add UI/RPC later if users need it. |
| Extension backends | Add the registry now, official HooksAPI registration after SQLite works. |
| CJK/substring search | Defer trigram index until basic FTS is stable. |
| Concurrency | Use short SQLite timeout and one transaction per Session rebuild. Avoid long startup locks. |

## Acceptance Criteria

- `session_search` works unchanged with default settings.
- Selecting `sqlite_fts` improves query execution through a derived SQLite FTS
  index while preserving current result shape.
- Deleting or corrupting the index does not lose Sessions and does not make
  recall unavailable if JSONL scanning can still answer.
- Runtime can select recall backends by name.
- The codebase has a documented recall domain separate from pinned memory and
  Session persistence.
