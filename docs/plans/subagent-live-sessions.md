# Subagent Live Sessions Plan

## Goal

Make sub-agent sessions behave like normal, reusable chat sessions in the UI while adding the metadata and live-run discovery needed to inspect them while they are running.

## Scope

In:
- Mark child sessions spawned through the `subagent` tool with session metadata.
- Keep those sessions normal and writable; do not hide them from the regular session list.
- Return active-run information from `chat.history` when the requested session currently has a running Run.
- Let ChatView attach to that active Run's SSE stream when a session is opened or when a run-start event arrives for the currently open session.
- Show a Sub-agent badge in the session drawer for marked sessions.
- Add focused backend and frontend tests.

Out:
- No durable Run persistence.
- No full parent-child graph store yet.
- No cross-session team dashboard yet.
- No workspace/file locking for parallel agents.

## Implementation Steps

- [x] Add sub-agent session metadata during spawn.
- [x] Include active-run descriptors in `chat.history` responses.
- [x] Normalize and display sub-agent session metadata in the WebUI session drawer.
- [x] Attach ChatView to active-run SSE streams for opened sessions and current-session run-start events.
- [x] Add or update focused tests for backend RPC, sub-agent metadata, session-list normalization, and ChatView active-run attach.
- [x] Run focused verification commands and record results.

## Validation

- [x] Backend sub-agent tests pass.
- [x] Backend RPC tests for chat history/session metadata pass.
- [x] Frontend tests for ChatView/session list pass.
- [x] Project quality checks are run or any skipped checks are documented.

Focused verification run:
- `python -m pytest tests/core/tools/test_subagent.py tests/server/test_rpc.py::test_chat_history_includes_active_run_descriptor tests/server/test_rpc.py::test_chat_history_loads_current_session_and_strips_reasoning_meta tests/server/test_phase4_webui_contract.py::test_phase4_bootstrap_agent_and_current_history -q` -> 40 passed.
- `npm test -- src/components/__tests__/ChatView.test.js src/lib/__tests__/sessionListView.test.js src/lib/__tests__/i18n.test.js --run` from `webui` -> 41 passed.
- `python scripts/quality.py core/subagents/subagents.py server/delegates.py tests/core/tools/test_subagent.py tests/server/test_rpc.py` -> all gates passed.
- `python scripts/quality-frontend.py webui/src/components/ChatView.svelte webui/src/components/SessionListDrawer.svelte webui/src/components/__tests__/ChatView.test.js webui/src/lib/sessionListView.js webui/src/lib/__tests__/sessionListView.test.js webui/src/lib/i18n.js webui/src/lib/__tests__/i18n.test.js` -> all gates passed.
- Backend focused tests were re-run after formatting -> 40 passed.
- Browser check on temporary data dir with only the `main` agent: session drawer showed the Sub-agent badge and parent metadata, the marked session opened normally, and the composer stayed writable.
- Real browser E2E on the normal app config with the `main` agent: `main` invoked the `subagent` tool, child session `77cc3aa5-0b70-459c-90ec-aa238c5183ea` completed with `SUBAGENT_BROWSER_TEST_OK`, `view session` opened it as a writable sub-agent session, and `session.list` returned `is_subagent_session: true` plus parent run/tool metadata.

## Notes

- Sub-agent sessions remain regular sessions. Metadata is only a UI/navigation aid.
- WebSocket remains the app-wide summary channel. SSE remains the per-Run live output channel.
- Background sessions should continue to receive coarse WebSocket updates, but ChatView should only keep SSE attached for the currently opened session.