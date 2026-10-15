# update_goal

The Agent's side of a Session goal: it marks the goal `complete`, `blocked` or
`paused`, which ends its automatic Runs. Owned by `core/tools/update_goal.py`;
state and transitions belong to `GoalService` (`automation/goals.md`).

## Contract

- Session Grant (`TOOL_ACTIVATION_SESSION_GRANT`): Chat grants it while the
  Session's goal is `active`, `blocked` or `paused`; `complete` and `/goal
  clear` withdraw it through the ordinary Tool-epoch announcements. Not in the
  catalog, never loaded on demand, so it has no summary.
- `status` is the only parameter. The handler validates it itself
  (`open_input_schema`, `handler_validates_arguments`).
- The handler calls `GoalService.update_from_agent(address, status, run_id)`.
  It records the calling Run's id, so the end of the Run that set `blocked`
  does not reactivate the goal.
- Display: `Goal complete`, `Goal blocked`, `Goal paused`.

## Results and errors

- `complete`: `Goal marked complete; no further automatic turns. Finish your reply to the user.`
- `blocked`: `Goal marked blocked; automatic turns continue after the user replies.`
- `paused`: `Goal paused. The user can resume it with /goal resume or delete it with /goal clear.`
- Same status: success, `The goal is already <status>; nothing changed.`
- Extra fields such as `summary` execute and append `<field> has no effect; tell the user in your reply.`
- Unknown status: `Nothing was changed. Unknown status "<value>". Use one of: complete, blocked, paused.`
- No goal (a race with `/goal clear`): `Nothing was changed. This session has no goal; only the user can set one with /goal.`
- Save failure: `Nothing was changed. The goal could not be updated: vBot could not save the session. Try the call again once; if it fails again, tell the user.`

Tolerance: `done`, `completed`, `achieved`, `finished` read as `complete`;
`pause`, `stop`, `stopped` as `paused`; `block`, `stuck` as `blocked`, silently
and case-insensitively. Other values are refused.

## Agent-facing text

Shape follows Codex (`update_goal`, `status`: `complete | blocked | paused`),
which many Models know. Unlike Codex there is no `summary` field: it repeated
the reply to the user and no next step reads it. Reviewed with
`vbot-agent-tools` on 2026-10-15; not yet evaluated with Models.

| Text | Reason |
|---|---|
| `Mark the session goal complete, blocked or paused, which stops its automatic turns.` | Without "stops its automatic turns" the Agent cannot tell that the call ends the loop and calls it as a progress log after every turn. |
| `Mark it complete only when you checked every part of the goal against the current state, not because you are stopping work.` | Premature completion is the most reported goal failure in Codex and Kilo, especially near a limit. |
| `blocked: ... ask the user in your reply.` | The goal reactivates on the user's next message; without a question the user does not know what to answer. |
| `paused: only when the user asks you to stop or pause the goal.` | A plain "stop" in chat did not end the loop in Codex, Hermes and Cursor; without the condition the Agent pauses on its own when work gets hard. |
| Result `paused` names `/goal clear` | "Forget the goal" reads as `paused`; the result lets the Agent tell the user how to delete it. |

## Source and tests

`core/tools/update_goal.py`; `tests/core/tools/test_update_goal.py`; grant:
`tests/core/chat/test_chat_goal_session.py`, `tests/core/runtime/test_runtime_wiring.py`.
