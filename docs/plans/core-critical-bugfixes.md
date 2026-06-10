# Plan: Core Critical Bugfixes (Chat/Provider)

**Goal:** Three confirmed critical bugs in `core/chat` and `core/providers` are fixed: sessions no longer break permanently after a cancelled/crashed tool cycle, `temperature: null` is never sent to providers (and provider defaults apply again), and httpx transport errors are always classified as vBot provider errors with retry + persisted error message.

**Context:** A bug hunt (2026-06-10) confirmed these three findings by reproduction:

1. **Dangling `tool_calls` brick sessions.** The assistant message carrying `tool_calls` is persisted *before* tools run ([core/chat/chat.py:659](../../core/chat/chat.py)). On run cancel (`/stop`) or an unexpected exception during tool dispatch, the already-computed tool results are discarded — `run.raise_if_cancelled()` in [core/chat/tool_dispatch.py:267](../../core/chat/tool_dispatch.py) and in the persist loop [core/chat/chat.py:692-695](../../core/chat/chat.py) raise before/between appends. JSONL then contains an assistant turn with `tool_calls` and no tool results. Verified: `_embed_notes_into_request` replays that turn unrepaired into every later provider request → OpenAI and Anthropic reject the history with 400 → every subsequent message in that session fails. A process kill mid-run produces the same state, so a write-side fix alone is not sufficient.
2. **`temperature: null` on the wire + dead provider defaults.** `ChatLoop` always passes `temperature=agent.temperature` ([core/chat/chat.py:889,911](../../core/chat/chat.py)) and `DEFAULT_TEMPERATURE` is `None` ([core/agents/agents.py:28](../../core/agents/agents.py)). Both base adapters apply caller kwargs last via `payload.update(request_kwargs)` without filtering `None` ([core/providers/openai_compatible.py:239](../../core/providers/openai_compatible.py), [core/providers/anthropic.py:210](../../core/providers/anthropic.py)). Reproduced: the Anthropic payload contains `"temperature": null` even though `resources/providers/anthropic.json` declares `defaults.temperature: 0.7` — the None kwarg clobbers the default. The GitHub Copilot payload builders already drop `None` values ([core/providers/github_copilot_messages.py:492](../../core/providers/github_copilot_messages.py)); the base adapters must behave the same.
3. **Unclassified httpx transport errors.** `send()`/`stream()` catch only `httpx.TimeoutException` and `httpx.ConnectError`. `httpx.ReadError` (non-streaming), `httpx.RemoteProtocolError` (what h11 raises on the typical mid-stream server disconnect), other `httpx.TransportError`s, and `response.json()` decode errors escape as raw exceptions. Consequences: `retry_async` does not retry (no `retryable` attr), and `ChatLoop._execute_run` handles them in the bare `except BaseException` branch ([core/chat/chat.py:461-463](../../core/chat/chat.py)) — **no error message is persisted** and the run fails generically. The stream loops catch `httpx.ReadError` but not `RemoteProtocolError`.

**Scope:**
- In: the three fixes above, their tests, and the matching spec updates (`.vorch/specs/chat.md`, `.vorch/specs/providers.md`).
- Out: all medium/low findings from the same bug hunt — tracked in `handoff2.md` at the repo root. No chat-layer API changes, no schema changes, no migrations.

**Assumptions & Constraints:**
- Read `.vorch/PROJECT.md`, `.vorch/specs/chat.md`, and `.vorch/specs/providers.md` before starting (session-start rule + domain specs).
- No legacy-compat branches: the dangling-tool-call repair is a *request-build-time* normalization (runtime resilience), not a stored-data migration — JSONL files are never rewritten.
- Quality gates per phase: `python scripts/quality.py <changed paths>` must be green; tests are written in the same task as the fix.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Session resilience | Cancel/crash mid-tool-cycle leaves a session that continues working; existing broken sessions work again on the next request |
| M2 | Clean payloads | No `None`-valued caller kwargs serialized by any base adapter; provider defaults apply when the agent leaves a value unset |
| M3 | Classified transport errors | Every httpx transport failure surfaces as `NetworkError`/`ProviderTimeoutError`/`ProviderError`, retried where retryable, persisted as a `role: "error"` message |

### Phase Breakdown

#### Phase 1: Repair dangling tool calls (read-side) + persist results on cancel (write-side)
**Goal of this phase:** A session history containing an assistant `tool_calls` turn without matching tool results can never reach a provider in that state, and the normal cancel path stops producing such histories in the first place.
**Can run in parallel with:** Phase 2 (zero file overlap).

- **Read-side repair (the core fix):** when request history is assembled, every assistant entry with `tool_calls` must be followed by exactly one tool-result entry per `tool_call_id`, in the assistant's original tool-call order, before any non-tool entry. For missing ones, synthesize request-only tool messages (never persisted) whose content is the JSON of the stable failure envelope — use `tool_failure(...)` from `core.tools` (envelope keys `ok/error/data/artifacts`, see [core/tools/tools.py:223](../../core/tools/tools.py)) with a code like `result_unavailable` and a message stating the tool run was interrupted before a result was recorded. Use the original tool call's `name`. Apply the repair in the shared history-build path (`_embed_notes_into_request` or a post-pass in `_build_request_messages`) so it covers **both** the normal path and the compaction-checkpoint tail path; it must not interfere with note deferral (synthetic notes still come after the completed tool block) or with `_restore_active_tool_continuation`. — read: [.vorch/specs/chat.md], files: [core/chat/messages.py, core/chat/chat.py]
- **Write-side: persist computed results even when cancel arrives.** In `_dispatch_tool_calls`, do not discard results that `execute_many` already returned: drop the post-dispatch `run.raise_if_cancelled()` ([core/chat/tool_dispatch.py:267](../../core/chat/tool_dispatch.py)). In `_send_until_final`'s persist loop ([core/chat/chat.py:692-695](../../core/chat/chat.py)), append **all** sibling tool messages before honoring cancellation (move the `raise_if_cancelled` after the loop). Cancelled tools already return `cancelled_by_user` failure envelopes through the executor, so persisted history stays truthful. The run must still end as `cancelled`. — files: [core/chat/tool_dispatch.py, core/chat/chat.py]
- **Tests** (same task, AAA, mirrored under `tests/core/chat/`): (a) history `[user, assistant(tool_calls), error]` → request history contains synthesized failure tool results immediately after the assistant turn (repro from the bug hunt); (b) partial results — 2 of 3 siblings persisted → only the missing one is synthesized, order preserved; (c) compaction-tail path gets the same repair; (d) cancel during tool dispatch → all sibling tool messages are persisted before the run ends cancelled; (e) repaired entries are never written to the session JSONL. — files: [tests/core/chat/test_messages.py, tests/core/chat/test_chat.py, tests/core/chat/test_tool_dispatch.py]
- **Spec update:** document the request-history invariant (every `tool_call_id` answered before the next non-tool message; synthesized failure results for interrupted cycles; persistence of sibling results on cancel) in `.vorch/specs/chat.md`. — files: [.vorch/specs/chat.md]

**Dependencies:** none.
**Done when:** the repro from the bug hunt (assistant `tool_calls` + no tool results in history) produces a valid provider request; a run cancelled mid-tool-cycle persists all sibling tool results; all listed tests pass; `python scripts/quality.py core/chat/ tests/core/chat/` is green.

#### Phase 2: Drop None-valued request kwargs in base adapters
**Goal of this phase:** `None`-valued caller kwargs mean "not specified": they are never serialized and never clobber provider defaults.
**Can run in parallel with:** Phase 1 (zero file overlap). **Not** parallel with Phase 3 (shared files).

- Filter `None` values out of `request_kwargs` at the top of `_build_payload` in **both** base adapters, before tools/reasoning extraction and before defaults are applied ([core/providers/openai_compatible.py:215-240](../../core/providers/openai_compatible.py), [core/providers/anthropic.py:164-211](../../core/providers/anthropic.py)). Subclasses that call `super()._build_payload` (OpenRouter, OpenCode Go, Mistral, MiniMax) inherit the fix; the Copilot/Codex payload builders already filter and stay untouched. No chat-layer change — `ChatLoop` keeps passing `temperature=agent.temperature`. — read: [.vorch/specs/providers.md], files: [core/providers/openai_compatible.py, core/providers/anthropic.py]
- **Tests:** (a) `temperature=None` → key absent from payload **and** the provider default (`defaults.temperature: 0.7`) applies via `setdefault`; (b) explicit `temperature=0.0` survives (falsy-but-not-None must not be dropped); (c) explicit `temperature=0.3` still overrides the default; (d) same assertions for the Anthropic payload builder. — files: [tests/core/providers/test_openai_compatible.py, tests/core/providers/test_anthropic.py]
- **Spec update:** add the convention to `.vorch/specs/providers.md`: "None-valued caller kwargs are treated as not specified — adapters drop them before building payloads; provider defaults then apply." — files: [.vorch/specs/providers.md]

**Dependencies:** none.
**Done when:** payload-builder tests show no `null` values for unset kwargs and working provider defaults; `python scripts/quality.py core/providers/ tests/core/providers/` is green.

#### Phase 3: Classify all httpx transport errors
**Goal of this phase:** Any httpx transport failure during provider requests becomes a classified vBot error (retryable where transient), so `retry_async` retries it and the chat loop persists a `role: "error"` message.
**Can run in parallel with:** none (shares `core/providers/openai_compatible.py` and `core/providers/anthropic.py` with Phase 2 — run after Phase 2).

- Broaden `wrap_network_error` in [core/providers/_http_shared.py:202-211](../../core/providers/_http_shared.py): `httpx.TimeoutException` → `ProviderTimeoutError`; **any other** `httpx.TransportError` (ConnectError, ReadError, WriteError, RemoteProtocolError, …) → `NetworkError`. Both stay retryable; `NetworkError` deliberately stays a non-`ProviderError` so it never triggers model fallback (existing invariant, [.vorch/specs/providers.md](../../.vorch/specs/providers.md) gotchas). — files: [core/providers/_http_shared.py]
- Replace every narrow `except httpx.TimeoutException` / `except httpx.ConnectError` pair around request submission with a single `except httpx.TransportError as exc: raise wrap_network_error(exc) from exc`, and widen the mid-stream `except httpx.ReadError` clauses to `except httpx.TransportError` (keeping the existing timeout wording). Affected sites (grep-verified): [core/providers/openai_compatible.py:291,355,403](../../core/providers/openai_compatible.py), [core/providers/anthropic.py:294,362,428](../../core/providers/anthropic.py), [core/providers/openai.py:298,324,363](../../core/providers/openai.py), [core/providers/github_copilot.py:199,225,263,290](../../core/providers/github_copilot.py), [core/providers/token_getter.py:156,187](../../core/providers/token_getter.py). Preserve the existing "mid-stream errors propagate / only connection establishment is retried" semantics — do not add new retry loops. — files: [core/providers/openai_compatible.py, core/providers/anthropic.py, core/providers/openai.py, core/providers/github_copilot.py, core/providers/token_getter.py]
- Classify malformed JSON bodies: wrap `response.json()` in the non-streaming send paths so a 2xx with an unparseable body raises `ProviderError(..., retryable=False)` (mirroring `parse_sse_json_data`) instead of a raw `json.JSONDecodeError`. — files: [core/providers/openai_compatible.py, core/providers/anthropic.py, core/providers/openai.py, core/providers/github_copilot.py]
- **Tests:** (a) `wrap_network_error` mapping table (ConnectError/ReadError/RemoteProtocolError → `NetworkError`; ConnectTimeout/ReadTimeout/PoolTimeout → `ProviderTimeoutError`); (b) mock transport raising `httpx.RemoteProtocolError` mid-SSE-stream → adapter raises `NetworkError`, not a raw httpx error; (c) non-streaming send over a transport that raises `httpx.ReadError` → `NetworkError` and `retry_async` retries it; (d) 200 response with invalid JSON → non-retryable `ProviderError`. — files: [tests/core/providers/test_http_shared.py, tests/core/providers/test_openai_compatible.py, tests/core/providers/test_anthropic.py]
- **Spec update:** note in `.vorch/specs/providers.md` that all httpx transport errors are wrapped (`TimeoutException` → timeout, rest → `NetworkError`) and malformed 2xx JSON is a fatal `ProviderError`. — files: [.vorch/specs/providers.md]

**Dependencies:** Phase 2 (shared files).
**Done when:** a simulated mid-stream `RemoteProtocolError` ends the run with a persisted `role: "error"` message of kind `network_error` (assert via the chat-loop path or via `_exception_to_error_kind(NetworkError(...))`); all listed tests pass; `python scripts/quality.py core/providers/ tests/core/providers/` is green.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Read-side repair fires on the live in-loop turn and double-answers a tool call | Low | High | Repair only synthesizes results for `tool_call_id`s with no following tool entry; mid-loop histories always contain all sibling results before the next request (tests b/c cover this) |
| Repair interacts badly with note deferral / compaction tail rebuild | Med | Med | Place repair in the shared assembly path and cover both paths with dedicated tests; keep synthesized entries request-only |
| Dropping `None` kwargs changes behavior for a provider that relied on explicit `null` | Low | Low | No vBot caller sends meaningful nulls today (verified: chat passes agent fields, compaction passes `0.0`); test (b) pins `0.0` survival |
| Widening to `httpx.TransportError` accidentally swallows `httpx.HTTPStatusError` | Low | Med | `HTTPStatusError` is not a `TransportError` (different branch of the httpx hierarchy); status handling stays with `classify_http_status` — add no `except httpx.HTTPError` clauses |
| `token_getter.py` widening changes OAuth refresh error semantics | Low | Med | Same wrap function, same retryability; existing token-getter tests must stay green |
