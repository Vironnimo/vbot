## Plan: `/handoff` slash command

**Goal:** Typing `/handoff` (optionally `/handoff <agent-id>`) makes the current agent write a structured handoff, then automatically creates a new session, injects the handoff as the first user message into it, and opens that session in the WebUI — streaming the receiving agent's response live.

**Context:** Users want a one-shot "hand this work over to a fresh session (or another agent)" action. The mechanism was decided as a **built-in slash command** (not a tool, not a skill). The chosen execution shape is **synchronous orchestration (Variant A)**: the command handler awaits the handoff-writing run, extracts its text, creates the new session, and triggers a run there — then returns the new session id so the existing `/new` UI auto-switch path takes over. This reuses maximal existing machinery; the only genuinely new backend logic is one command handler.

Key existing facts the implementation relies on (verified in code — do not re-derive):
- Slash commands are recognized **before** a run in [`core/chat/commands.py`](../../core/chat/commands.py) `CommandDispatcher.dispatch()`, which returns `CommandHandled` (immediate reply) or `CommandAction` (accessor-level work). The action is executed server-side in [`server/rpc/chat_methods.py`](../../server/rpc/chat_methods.py) `_handle_command_action()`.
- A command handler **can already start model runs** — `compact` and `retry` do (`_handle_compact_command`, `_retry_chat_for_ids`).
- `ChatLoop._execute_run` with `internal=True` adds the message as a **system-reminder note** (`session.add_note(content)`), NOT a visible user turn — see [`core/chat/chat.py:338-341`](../../core/chat/chat.py#L338). The model then writes its answer as a normal visible assistant turn.
- `TriggerService.trigger_run(agent_id, message, session_id=..., internal=...)` ([`core/automation/automation.py:39`](../../core/automation/automation.py#L39)) starts (or queues) a run and returns the `Run`. It is reachable as `state.runtime.trigger_service` (compact uses it the same way).
- `Run.wait()` waits for terminal state and returns the final assistant `ChatMessage` (see `_send_chat` at [`chat_methods.py:255`](../../server/rpc/chat_methods.py#L255)). `ChatMessage.content` ([`core/chat/messages.py:102`](../../core/chat/messages.py#L102)) is `str | list[ContentBlock] | None`.
- `_create_session(state, {"agent_id", "make_current": True})` ([`server/rpc/agent_methods.py:122`](../../server/rpc/agent_methods.py#L122)) creates the session, optionally sets it current, and returns `{"agent_id", "session_id"}`.
- `/new` returns `data: {command: "new", session_id}`; the WebUI reads it in `newSessionIdFromCommandResponse` and calls `switchToCurrentSession(agent.id, newSessionId)` ([`ChatView.svelte:222`](../../webui/src/components/ChatView.svelte#L222) and [`:530-534`](../../webui/src/components/ChatView.svelte#L530)). `loadHistoryForSession` then calls `attachRunStream(history.active_run)` ([`:315`](../../webui/src/components/ChatView.svelte#L315)) — so switching into a session that already has an active run **auto-subscribes to its stream**.

**Scope:**
- In: same-agent handoff and cross-agent handoff (`/handoff <agent-id>`); writing the handoff via an internal run; creating + opening the new session; injecting the handoff as a user message that the receiving agent runs on (auto-run); `/help` listing; i18n; tests; spec updates.
- Out: live-streaming the handoff *as it is written* in the source session (Variant B — explicitly deferred); persisting the handoff to a separate file; a "review before send" / no-auto-run mode (auto-run is the chosen default); a configurable handoff prompt UI (the instruction is a code constant for now).

**Assumptions & Constraints:**
- No legacy/back-compat shims (project rule). New format only.
- Every user-visible string goes through i18n (backend `utils/` i18n, frontend `webui/src/lib/i18n.js`). The internal handoff *instruction* sent to the model is NOT user-visible (it is a note) and stays a plain constant; the user-facing *reply* ("Handoff sent…") and any frontend text MUST be i18n.
- The handoff-writing run blocks the `/handoff` RPC call until it finishes (acceptable; same pattern as `/compact`). See Risks for the timeout consideration.

---

### Data contract (shared across phases — implement exactly)

The `/handoff` command, when handled, returns a `CommandHandled`-shaped RPC response:

```
{
  "command_handled": true,
  "reply": "<i18n: handoff sent message>",
  "data": {
    "command": "handoff",
    "session_id": "<new session id>",
    "agent_id": "<target agent id>"   // current agent id when no target given
  }
}
```

The WebUI switches to `data.session_id` **under `data.agent_id`** when `data.command === "handoff"`.

---

### Handoff instruction template (`HANDOFF_INSTRUCTION`)

This is the heart of the feature — the prompt that produces the handoff. Define it as a module-level constant in [`server/rpc/chat_methods.py`](../../server/rpc/chat_methods.py) and pass it as the `internal=True` note message of the handoff-writing run. It is **plain text, NOT i18n** (it is sent to the model, never shown to the user). Paste it **verbatim**; do not paraphrase or trim sections — the wording is deliberate.

```text
You are handing off this conversation to another agent who will continue it in a
fresh session with none of its context. Write a handoff so they can carry on
seamlessly, as if they had been here the whole time.

Capture whatever actually matters in this conversation so far — what it has been
about, what has been said, established, or decided, and where things currently
stand. What that includes depends entirely on the conversation: it might be a
task in progress, a discussion, a decision being worked through, or anything
else. Include only what is genuinely relevant here and leave out the rest; do not
force it into a fixed structure or invent things that are not there.

Write it entirely from this conversation — do not use tools or go check anything.
Write it as a briefing to the next agent, in the language of this conversation,
and output only the handoff itself, with no preamble and no sign-off, because
your reply becomes their first message.
```

---

**Phases:**

### Phase 1: Command recognition + argument parsing
**Goal of this phase:** `dispatch()` recognizes `/handoff` and optional `/handoff <agent-id>` and returns a `CommandAction` carrying the (optional) target agent id. No orchestration yet.
**Can run in parallel with:** Phase 3 (different files).

- Add `"handoff"` to the `CommandActionName` `Literal` and add an optional field to `CommandAction` for the target agent — files: [core/chat/commands.py](../../core/chat/commands.py)
  - Change `CommandActionName = Literal["compact", "new_session", "retry_last_turn"]` → add `"handoff"`.
  - Add field to the frozen dataclass `CommandAction`: `argument: str | None = None`. Keep it generic (named `argument`, not `agent_id`) so the dataclass stays reusable; document with a short docstring line that for `handoff` it is the target agent id or `None`.
- Add `/handoff` to `BUILT_IN_COMMANDS` with a one-line description (so `/help` lists it) — files: [core/chat/commands.py](../../core/chat/commands.py)
  - e.g. `"handoff": "Write a handoff and start a new session (optionally for another agent: /handoff <agent-id>)."`
- Parse `/handoff [agent-id]` in `dispatch()` — files: [core/chat/commands.py](../../core/chat/commands.py)
  - Current `dispatch()` does an exact, lowercased, whole-string dict lookup. Extend it: before the existing exact-match lookup, compute the first whitespace-delimited token of `message_text.strip()`. If that token, lowercased, equals `/handoff`, parse the remainder:
    - Strip the remainder; if non-empty, that is the target agent id (**preserve original case** — do not lowercase agent ids); if empty, `argument=None`.
    - If there is more than one whitespace-separated argument token, treat it as not-a-command / invalid and fall through to `NotACommand()` (a handoff takes at most one agent id). Keep it simple: only 0 or 1 trailing token is valid.
    - Return `CommandAction(name="handoff", argument=<agent_id_or_None>)`.
  - Leave the existing exact-match dispatch for `/compact`, `/new`, `/retry`, `/status`, `/stop`, `/help` unchanged. (`/handoff` is handled by the new branch above and is intentionally NOT added to the `self._commands` exact-match dict, since it takes arguments.)
- Tests — files: [tests/core/chat/test_commands.py](../../tests/core/chat/test_commands.py)
  - `/handoff` → `CommandAction(name="handoff", argument=None)`.
  - `/handoff coder` → `CommandAction(name="handoff", argument="coder")`.
  - Case preservation: `/handoff MyAgent` → `argument == "MyAgent"`.
  - Leading/trailing whitespace tolerated: `"  /handoff coder  "` works.
  - `/handoff a b` (two args) → `NotACommand`.
  - `/handoff` appears in `BUILT_IN_COMMANDS` (so `/help` output includes it).

**Dependencies:** none.
**Done when:** the new tests in `test_commands.py` pass; `python scripts/quality.py core/chat/commands.py tests/core/chat/test_commands.py` is green.

---

### Phase 2: Server orchestration (`_handle_handoff_command`)
**Goal of this phase:** Executing the `handoff` action writes the handoff, creates the new session, injects it, and returns the data contract.
**Can run in parallel with:** Phase 3 (different files).

- Define the handoff instruction constant — files: [server/rpc/chat_methods.py](../../server/rpc/chat_methods.py)
  - Add a module-level constant `HANDOFF_INSTRUCTION` containing the **verbatim** text from the "Handoff instruction template" section above. Plain string, NOT i18n (it is sent to the model as a note, never shown to the user). Do not paraphrase, trim, or reorder its sections — the wording is deliberate.
- Add `_handle_handoff_command(state, agent_id, session_id, target_agent_id)` and wire it into `_handle_command_action`'s `match` — files: [server/rpc/chat_methods.py](../../server/rpc/chat_methods.py)
  - In `_handle_command_action`, add `case "handoff":` → `return await _handle_handoff_command(state, agent_id, session_id, command_action.argument)`. Note: `_handle_command_action` is already `async`.
  - Handler logic (in order):
    1. **Busy guard (source session):** if `_state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)` is not `None`, return `_command_handled_response(<i18n: cannot handoff while a run is active>)` — mirror `_handle_new_session_command` ([chat_methods.py:179](../../server/rpc/chat_methods.py#L179)).
    2. **Resolve target agent:** `target = (target_agent_id or "").strip() or agent_id`. If `target != agent_id`, validate it exists via `state.runtime.agents.get(target)` and map a missing-agent error through `_map_expected_error` to a clean reply (do not 500). (`_create_session` also validates, but validating here lets you fail before writing the handoff.)
    3. **Write the handoff:** `handoff_run = await state.runtime.trigger_service.trigger_run(agent_id, HANDOFF_INSTRUCTION, session_id=session_id, internal=True)` then `handoff_message = await handoff_run.wait()`. Wrap in try/except → `_map_expected_error`.
    4. **Extract text:** read `handoff_message.content`. If it is a `str`, use it. If it is a `list` of content blocks, join the text of `TextBlock` items (import `TextBlock` from `core.chat.content_blocks`, already imported in this module). If the result is empty/None, return an i18n error reply ("Handoff could not be generated.") and do NOT create a session.
    5. **Create the new session under the target:** `response = _create_session(state, {"agent_id": target, "make_current": True})`; `new_session_id = _required_string(response, "session_id")`.
    6. **Inject (auto-run):** `await state.runtime.trigger_service.trigger_run(target, handoff_text, session_id=new_session_id)`. Do NOT `await .wait()` — return while the receiving agent runs; the UI attaches to the active run on switch. Bridge the started run to the event bus for WS lifecycle parity if straightforward — check how `_send_chat`/`_stream_chat` use `_bridge_run_to_event_bus(state, run)` and apply the same to the returned run if `trigger_run` does not already bridge. (Functionally the UI works without it because it reads `active_run` from `chat.history`; treat bridging as parity polish, not a blocker.)
    7. **Return:** `_command_handled_response(CommandHandled(reply=<i18n: handoff sent, includes target + new_session_id>, data={"command": "handoff", "session_id": new_session_id, "agent_id": target}))`.
  - Reuse existing helpers/imports already in the file: `CommandHandled`, `_create_session`, `_required_string`, `_command_handled_response`, `_map_expected_error`, `_state_chat_runs`, `TextBlock`.
- Add backend i18n strings for the user-facing replies — files: backend i18n catalog under [core/utils/](../../core/utils/) (find the existing translation mechanism used by other backend user-facing strings; follow that exact pattern — keys + English fallback). Strings needed:
  - handoff active-run guard message
  - handoff-generation-failed message
  - handoff-sent confirmation (with placeholders for target agent id and session id)
  - (If backend command replies elsewhere are currently plain English literals rather than i18n, match the surrounding convention in `commands.py`/`chat_methods.py` instead of inventing a new i18n path — read how `_handle_new_session_command` and `_handle_help` produce their text and stay consistent.)
- Tests — files: [tests/server/test_rpc.py](../../tests/server/test_rpc.py) (or [tests/server/test_delegates.py](../../tests/server/test_delegates.py) if that is where command-action dispatch is tested — match where `new_session`/`retry` command tests live)
  - `/handoff` on an idle session: a new session is created under the same agent, it becomes current, and the response `data` is `{command: "handoff", session_id: <new>, agent_id: <same>}`. The new session's history contains the injected handoff as a user message.
  - `/handoff <other-agent>`: new session is created under `<other-agent>` and becomes that agent's current session; `data.agent_id == "<other-agent>"`.
  - `/handoff <missing-agent>`: returns a clean `command_handled` error reply, no session created, no 500.
  - `/handoff` while a run is active on the source session: returns the busy-guard reply, no new session.
  - Use the existing test harness/fakes for `trigger_service`/chat loop the way the `retry`/`compact`/`new` command tests do — do not call real providers. If the harness runs a fake chat loop, assert the handoff note + injected message via the fake's recorded calls.

**Dependencies:** Phase 1 (`CommandAction.argument`, `name == "handoff"`).
**Done when:** the new RPC tests pass; `python scripts/quality.py server/rpc/chat_methods.py tests/server/test_rpc.py` (and the i18n file's path) is green.

---

### Phase 3: WebUI switch (incl. cross-agent)
**Goal of this phase:** When a `chat.stream`/`chat.send` returns the `handoff` command response, the UI switches to the new session **under the response's `agent_id`** and (via the existing history-load path) attaches to the already-running receiving agent's stream.
**Can run in parallel with:** Phase 2 (only touches frontend files; relies solely on the documented data contract above, not on Phase 2's code).

- Extend command-response handling to recognize `command === "handoff"` and switch under the response agent — files: [webui/src/components/ChatView.svelte](../../webui/src/components/ChatView.svelte)
  - Today `newSessionIdFromCommandResponse` ([:222](../../webui/src/components/ChatView.svelte#L222)) only accepts `data.command === 'new'`, and the caller ([:534](../../webui/src/components/ChatView.svelte#L534)) switches with `switchToCurrentSession(agent.id, newSessionId)` — i.e. always the current agent.
  - Add handling so that for `data.command === 'handoff'`, the UI switches to `data.session_id` under `data.agent_id` (which may differ from the current agent). Concretely: make the command-response branch read both `session_id` and an optional `agent_id` from `data`, and call `switchToCurrentSession(targetAgentId || agent.id, newSessionId)`. Keep `command === 'new'` behaving exactly as before (no `agent_id` → current agent). Implement cleanly (e.g. a small helper returning `{ sessionId, agentId }` for both `new` and `handoff`), without breaking the existing `/new` and `/compact` branches.
  - Verify `switchToCurrentSession(agentId, sessionId)` already supports an agent id different from the currently selected agent (it takes `agentId` as its first param — [:485](../../webui/src/components/ChatView.svelte#L485)). If switching agents needs the agent to also be selected in the surrounding agent state, handle that within this change (select the target agent, then switch session). Confirm against how the agent-selection state is updated elsewhere in this component.
- Frontend i18n: no new visible strings are strictly required (the reply text comes from the backend via `actionInfo`), but if you add any UI label, route it through [webui/src/lib/i18n.js](../../webui/src/lib/i18n.js) with an English fallback.
- Tests — files: frontend tests mirroring the component, e.g. [webui/src/components/__tests__/](../../webui/src/components/__tests__/) or the existing `sessionListView`/command-response test location — match where the `/new` command-switch behavior is tested.
  - A `handoff` command response with `data.agent_id` equal to the current agent switches to the new session.
  - A `handoff` command response with a **different** `data.agent_id` switches to that agent's new session.
  - A `new` command response still behaves as before (regression guard).

**Dependencies:** the data contract above (documented; does not require Phase 2 code to be merged). File-scope does not overlap Phase 1/2.
**Done when:** frontend tests pass; `python scripts/quality-frontend.py webui/src/components/ChatView.svelte` (and the test file) is green, including the build step.

---

### Phase 4: Specs & docs
**Goal of this phase:** Keep specs accurate (project rule: docs maintained with the change).
**Can run in parallel with:** nothing that edits the same files; run after Phases 1–3 land so it documents the final shape.

- Document the `/handoff` built-in command — files: [.vorch/specs/chat.md](../../.vorch/specs/chat.md)
  - Read [.vorch/workflows/spec-workflow.md](../../.vorch/workflows/spec-workflow.md) first. Add a factual note that `/handoff [agent-id]` is a built-in command that (1) writes a handoff via an internal note-driven run, (2) creates a new session under the target agent (default: current), (3) injects the handoff as a user message and auto-runs the receiving agent, (4) returns `data: {command: "handoff", session_id, agent_id}`. Reference `core/chat/commands.py` (recognition) and `server/rpc/chat_methods.py` (`_handle_handoff_command`). Keep it short; no API dumps.
- Document the UI switch contract — files: [.vorch/specs/webui.md](../../.vorch/specs/webui.md)
  - One line: the chat command-response handler switches to `data.session_id` under `data.agent_id` for `command === "handoff"` (cross-agent capable), reusing the `/new` auto-switch + active-run attach path.
- If the PROJECT.md Context note "Built-in commands and skill triggers are separate layers" needs a mention that one built-in command now takes an argument and starts model runs, update it minimally — files: [.vorch/PROJECT.md](../../.vorch/PROJECT.md) (only if it improves accuracy; otherwise skip).

**Dependencies:** Phases 1–3 (documents their final behavior).
**Done when:** chat.md and webui.md reflect the implemented behavior; claims point at real source.

---

**Done when (overall):**
- `/handoff` in the WebUI: the agent writes a handoff, a new session opens automatically, the handoff appears as the first user message, and the receiving agent's response streams in live.
- `/handoff <agent-id>` opens the new session under that other agent.
- `/handoff` while a run is active is rejected with a clear message.
- `/help` lists `/handoff`.
- `python scripts/quality.py` and `python scripts/quality-frontend.py` are fully green.

**Risks / Assumptions:**
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Handoff run is slow → `/handoff` RPC pends 10–60s and may hit a request timeout | Med | Med | Same blocking pattern as `/compact` is already accepted. If a timeout shows up, the documented upgrade is Variant B (return a streaming run + a `run_completed` listener that does steps 5–6, plus a WS "open session" push). Out of scope here; note it in `chat.md` as the known evolution. |
| Switching into a session that already has an active run doesn't attach the stream | Low | Med | Verified it does: `loadHistoryForSession` calls `attachRunStream(history.active_run)`. Frontend test should assert the switch happens; manual verify the live stream once. |
| Cross-agent switch needs the surrounding agent-selection state updated, not just the session | Med | Low | Phase 3 explicitly checks `switchToCurrentSession` and selects the target agent if required. |
| `trigger_run` does not bridge to the event bus → WS lifecycle summary missing for the inject run | Low | Low | UI reads `active_run` from `chat.history`, so streaming still works. Bridge if simple (Phase 2 step 6); otherwise accept. |
| Backend command replies are plain literals, not i18n, in current code | Med | Low | Match the existing convention in `commands.py`/`chat_methods.py` rather than forcing a new i18n path; keep `/handoff` consistent with `/new` and `/help`. |
| Model emits preamble around the handoff ("Here's your handoff:…") which then becomes the next user message | Med | Low | `HANDOFF_INSTRUCTION` explicitly says "output only the handoff text, no preamble." Acceptable if slightly imperfect. |

**Open decision (resolved, flagged for confirmation):**
- **Auto-run vs deposit-only in the new session.** Chosen: **auto-run** (the receiving agent immediately works off the handoff) because the user described the handoff being "sent as a user message," and `trigger_run` runs the model on send. Alternative (deposit the user message without running) would require appending to the session without a run and is deferred. If the user wants a review-before-run step, that becomes a follow-up.
