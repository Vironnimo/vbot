# Chat External Runs

How `ChatLoop.start_external_run` (`core/chat/_external_run.py`) records a conversation whose turns a Model outside the Agentic Loop produces, today the voice model of a Live call (`model_tasks/live.md` -> Live Agents and Sessions).

- `start_external_run` creates a new Session titled by the caller and one Run in it (default kind `live`, no Agent activity) and returns an `ExternalRun`.
- The owner stores what the user and the Model said (`record_user`, `record_assistant`; the Assistant messages carry the caller's `model` label) and notes about what it told the Model (`record_note`).
- `run_tool` runs each Tool call the Model made through the same Tool dispatch as a Chat Run: the Agent's effective Tools plus the caller's `extra_tools`, offered as `tool_definitions`; events, stored Tool Results and the Tool name resolution of other harnesses' spellings as in a Chat Run. Each call is stored once it finished, as one Assistant turn with the call and its result after what was said meanwhile; calls can run concurrently.
- `finish(failure=)` waits up to 5 s for calls still running and stores those still running then with the result `tool_stopped` (their later result is not stored), then ends the Run (completed, or failed with the failure stored as a visible error). A call after the end, or cancelled while running, gets the result `tool_stopped`; a cancel or deadline that lands once a call finished, while it is being stored, returns that call's result.
- `discard()` deletes the Session when the conversation never started. A user's cancel of the Run calls the owner's `on_cancel`, which ends the conversation.
- Tests: `tests/core/chat/test_external_run.py`.
