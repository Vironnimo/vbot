# Plan: System Reminders

**Goal:** The chat layer can persist `note` entries in session JSONL files and embed them as synthetic `<system-reminder>` user messages when building provider requests — both at run start and between tool iterations — so kernel-internal producers (including tools) can inform the model of background events.

**Context:** Background use cases (long-running background tools, sub-agents) need a way to inform the model of something that happened without it being a real user message. The reminder appears to the model as a user turn; the normal UI never shows it. No producer exists yet — this is pure infrastructure.

**Decisions made:**
- Injection: kernel-internal only — callers use `context.note_hook("text")` (tools) or `session.add_note("text")` (other kernel code) on the same `ChatSession` instance held by the chat loop
- Hook pattern: `note_hook: Callable[[str], None] | None` on `ToolContext` / `ToolExecutionConfig` — identical pattern to `emit_hook` and `cancellation_hook`; no full session passed to tools
- Pending tracking: in-memory buffer on `ChatSession` — `add_note()` writes to JSONL AND enqueues; drain before each provider request, no file re-read
- Mid-loop injection: fully supported — drain before every model request (initial and tool-result turns)
- Notes included in `ChatSession.load()` — future debug UI needs only frontend work

**Scope:**
- In: `ChatMessage` note role · `ChatSession.add_note()` + `drain_pending_notes()` · `note_hook` on `ToolContext` / `ToolExecutionConfig` · embedding in initial request build and each loop iteration · tests
- Out: RPC endpoint, server surface, UI debug view

---

## Phases

### Phase 1: Note role in `ChatMessage`

**Goal:** `ChatMessage` accepts `role: "note"`, validates correctly, and round-trips through `to_dict()` / `from_dict()`.

- [ ] Extend `MessageRole` type alias: `Literal["system", "user", "assistant", "tool", "note"]`

- [ ] Add `ChatMessage.note(content: str, *, timestamp: datetime | None = None) -> ChatMessage` — sets `role="note"`, `content=content`, all other fields `None`.

- [ ] Update `_require_role()` — add `"note"` to accepted set.

- [ ] Add `_validate_note_message(message: ChatMessage)` — requires `content` is not `None`, rejects all other optional fields (`model`, `reasoning`, `reasoning_meta`, `usage`, `tool_calls`, `tool_call_id`, `name`) via `_reject_fields()`.

- [ ] Add `case "note"` arm to `ChatMessage.validate()`.

- [ ] Tests in `tests/core/chat/test_chat_messages.py`:
  - `note()` sets `role`, `content`; all other fields are `None`
  - `to_dict()` / `from_dict()` round-trip preserves `role: "note"` and `content`
  - `validate()` raises on note with `model`, `usage`, `tool_calls`, etc.

**Files:** `core/chat/chat.py`, `tests/core/chat/test_chat_messages.py`
**Done when:** `ChatMessage.from_dict({"id":"x","timestamp":"2026-01-01T00:00:00+00:00","role":"note","content":"hi"})` succeeds and new tests pass.

---

### Phase 2: Session API (depends on Phase 1)

**Goal:** `ChatSession` holds a pending-note buffer; producers write via `add_note()`; the chat loop drains via `drain_pending_notes()`.

- [ ] Add `_pending_notes: collections.deque[ChatMessage]` to `ChatSession.__init__()`.

- [ ] Add `ChatSession.add_note(content: str) -> None`:
  Creates `ChatMessage.note(content)`, calls `self.append()` (JSONL), pushes onto `_pending_notes`.

- [ ] Add `ChatSession.drain_pending_notes() -> list[ChatMessage]`:
  Returns all enqueued notes and clears the deque. Returns `[]` when empty.

- [ ] Tests in `tests/core/chat/test_sessions.py`:
  - `add_note()` appends a valid `role: "note"` JSONL line
  - `load()` includes the note in its output
  - `drain_pending_notes()` returns added notes and leaves deque empty
  - Second drain returns `[]`

**Files:** `core/chat/chat.py`, `tests/core/chat/test_sessions.py`
**Done when:** all new session tests pass.

---

### Phase 3: `note_hook` on ToolContext / ToolExecutionConfig (depends on Phase 2)

**Goal:** Tools can inject a reminder without knowing anything about sessions — same pattern as `emit_hook`.

- [ ] Add `note_hook: Callable[[str], None] | None = None` to `ToolExecutionConfig` (frozen dataclass).

- [ ] Add `note_hook: Callable[[str], None] | None = None` to `ToolContext` (frozen dataclass).

- [ ] Add `ToolContext.add_note(content: str) -> None` convenience method — calls `self.note_hook(content)` if set, silently does nothing if `None`.

- [ ] In `ChatLoop._dispatch_tool_calls()`, pass `note_hook=session.add_note` when building `ToolExecutionConfig`. The `session` variable is already in scope in `_execute_run()` — thread it through to `_dispatch_tool_calls()` as a parameter.

- [ ] In `ToolExecutor._build_context()` (where `ToolContext` is constructed from `ToolExecutionConfig`), pass `note_hook` through.

- [ ] Tests in `tests/core/tools/` (existing test module for tools):
  - `ToolContext.add_note()` calls the hook when set
  - `ToolContext.add_note()` does nothing silently when `note_hook` is `None`

**Files:** `core/tools/tools.py`, `core/chat/chat.py`, `tests/core/tools/` (existing test file)
**Done when:** `context.add_note("reminder")` in a tool handler reaches `session.add_note()` without the tool holding a session reference.

---

### Phase 4: Embedding — initial build and mid-loop injection (depends on Phase 2)

**Goal:** Pending notes become synthetic `<system-reminder>` user messages in provider requests — at run start and before every subsequent model request.

#### Helpers

- [ ] Add `_notes_to_synthetic_user_message(notes: list[ChatMessage]) -> JsonObject`:
  ```
  {"role": "user", "content": "<system-reminder>\n{note1}\n</system-reminder>\n<system-reminder>\n{note2}\n</system-reminder>"}
  ```
  Never persisted.

- [ ] Add `_embed_notes_into_request(messages: list[ChatMessage]) -> list[JsonObject]`:
  Scans messages in order. Consecutive notes flush as one synthetic user message. Non-notes pass through `_message_to_request_dict()`. Notes never reach `_message_to_request_dict()`.

#### Initial request build

- [ ] Update `_build_request_messages()`:
  ```python
  history = _embed_notes_into_request(session.load())
  return [system_message.to_dict(), *history]
  ```

#### Mid-loop injection

- [ ] In `_send_until_final()`, drain and embed before every `_send_assistant_request()` call:
  ```python
  pending = session.drain_pending_notes()
  if pending:
      messages.append(_notes_to_synthetic_user_message(pending))
  ```
  The `session` instance is passed in as a parameter (thread it through from `_execute_run()`).

#### Tests in `tests/core/chat/test_chat_loop.py`

- [ ] Note present before user turn → embedded as synthetic user message in provider request
- [ ] Multiple consecutive notes → one synthetic user message with multiple blocks
- [ ] `add_note()` called between tool iterations → appears in the next model request in that same run
- [ ] Notes never appear as `role: "note"` in messages sent to the adapter
- [ ] Sessions with no notes → identical request output to current behaviour (regression)

**Files:** `core/chat/chat.py`, `tests/core/chat/test_chat_loop.py`
**Done when:** all embedding tests pass; adapter never receives `role: "note"`.

---

## Done when

- `ChatMessage.note("x")` constructs, validates, round-trips
- `session.add_note("x")` persists to JSONL and enqueues in-memory
- `session.drain_pending_notes()` returns queued notes and clears buffer
- `context.add_note("x")` in a tool reaches the session without the tool holding a session reference
- Notes in JSONL history embedded as `<system-reminder>` blocks in provider requests
- Notes added mid-run via `add_note()` appear in the next model request within that run
- `python scripts/quality.py core/chat/ core/tools/` passes

## Risks / Assumptions

- **Two consecutive user messages** (`<system-reminder>` + real user turn): doc explicitly states this is valid for all providers.
- **`_dispatch_tool_calls()` needs `session` threaded through** from `_execute_run()` — small refactor, no logic change.
- **Spec update needed:** `.vorch/specs/chat.md` and `.vorch/specs/tools.md` must be updated to document the new APIs. Orchestrator's responsibility.
