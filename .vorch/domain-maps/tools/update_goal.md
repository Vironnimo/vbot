# Goal Tools

`create_goal` starts a Session Goal from a direct user request. `update_goal`
marks an open goal complete, blocked or paused. State, continuation and user
commands belong to `GoalService` (`automation/goals.md`); both Tools delegate
there and register at Runtime bootstrap after the service is built.

## Access and contract

- `create_goal` is configurable and catalog-visible. The user can select or
  deny it for an Agent; On-demand Tools use its declared summary. Its required
  `objective` names the requested end result.
- Creation needs direct human input in the exact current Run, including
  delivered steering into an originally automatic Run, outside
  Sub-Agent Sessions and group conversations. It never replaces an open goal
  or changes the 30-turn default; a completed goal can be replaced. Creating
  inside a Run starts no second Run.
  Chat supplies `ToolContext.direct_user_input`; Model arguments never set it.
- `update_goal` remains a Session Grant while the goal is active, blocked or
  paused, independent of `create_goal` selection. It is catalog-hidden and
  never on demand. Explicit denials, fixed sets and Tool access `none` retain
  shared policy semantics (`tools/access-policy.md`).
- `status` is required: complete, blocked or paused. `blocked_reason` is
  required for a new blocked status and ignored otherwise. The handler keeps
  `open_input_schema` and validates status itself. Three automatic turns are
  required before blocking; whether the same condition persisted is Model
  judgment. An unchanged status is an idempotent success.
- Description and parameter changes of `update_goal` are announced to an
  existing prompt epoch, including the blocker field and policy.
- Goal start notes, continuation context and Compaction retention belong to
  `automation/goals.md`; Chat refreshes the Session Grant inside the creating
  Run through ordinary Tool-epoch announcements.

## Results, failures and tolerance

Creation returns `Goal started.` and records the goal-set note and visible
goal row. Refusals are `goal_exists` (an open goal), `invalid_arguments` (empty
objective) and `goal_creation_not_allowed` (non-user scope); each says nothing
changed and gives the next action. Unexpected failures are
`goal_creation_failed`, asking the Agent to continue the task and report that
automatic continuation could not be enabled.

An update confirms its terminal status and directs the closing reply: verified
outcome and results for completion, completed work and concrete missing input
for blocking. Both direct the Agent to use Session evidence and finish without
more Tool calls. A pause names `/goal resume` and `/goal clear`. An unchanged
status changes nothing. Extra fields execute with
`<field> has no effect; tell the user in your reply.`

Update refusals: `invalid_arguments` (unknown status or missing blocker),
`goal_blocked_too_early` (fewer than 3 automatic turns), `no_goal` (a race with
clear). `goal_update_failed` reports a save failure: retry once, then tell the
user. No-goal feedback names the user's `/goal`, since the Agent may lack
`create_goal`.

Status aliases are case-insensitive: done/completed/achieved/finished ->
complete; pause/stop/stopped -> paused; block/stuck -> blocked. Scalar argument
representations follow shared repair. The blocker is stored in goal metadata,
not logged or echoed as an instruction.

## Agent-facing text

The DSH-derived proposal was approved on 2026-11-05. Creation selection and
the status Session Grant deliberately stay separate; no reduced duplicate
Tool or broadening of shared Agent policy is needed.

| Text | Reason |
|---|---|
| `Keep this Session working on an objective through automatic follow-up turns.` | Explains that creation enables work beyond the current turn. |
| `Use it when the direct human request is a long-running objective, even if the user did not say "goal".` | Allows clear task intent without a slash command or particular language. |
| `Skip tasks that fit in one turn.` | Keeps bounded requests out of automatic continuation. |
| Summary `Start a goal for a long-running user request.` | Makes that purpose discoverable when the definition is on demand. |
| Objective `The requested end result, inferred from the user's direct request.` | Elicits the user's result rather than progress or a self-assigned objective. |
| `Goal started.` | Confirms the effect without repeating the goal-set note or submitted objective. |
| `Mark the Session goal complete, blocked or paused, stopping its automatic turns.` | Identifies terminal control rather than a progress log. |
| Status `complete: verify every part of the objective against the current result before marking it complete.` | Preserves the whole-goal check, including completion before the first automatic turn. |
| Status `blocked: the same condition has prevented progress for at least 3 consecutive automatic turns, and no useful step remains without user input or an external change.` | Prevents an immediate give-up after a difficult attempt; the kernel enforces the count and the Agent judges the condition. |
| `Difficulty or uncertainty alone is not a blocker.` | Keeps useful investigation and alternative approaches inside the goal. |
| Status `paused: only when the user asks you to stop or pause the goal.` | Preserves user control; naming pause without a condition previously invited unsolicited pauses. |
| `The condition preventing further progress.` / `Required for blocked; omit otherwise.` | Captures the actual blocker without soliciting an unused field for other statuses. |
| Result `Goal marked complete; no further automatic turns.` | Confirms that continuation stopped. |
| `Tell the user what was accomplished, how it was verified, and where to find the results.` | Produces a reviewable closing answer instead of ending at the status call. |
| `Say what the user needs to review or do next, if anything.` | Names a real user action without inventing one. |
| Result `Goal marked blocked; automatic turns resume after the user replies.` | Explains vBot's existing reactivation, which differs from DSH's explicit resume. |
| `Tell the user what is done, which condition prevents progress, what you tried, and exactly what is needed to continue.` | Makes the blocker actionable for the user. |
| `Base the report on this Session's evidence; identify missing information instead of inventing it.` | Grounds both terminal reports in actual work. |
| `Finish your reply without further Tool calls.` | Ends the autonomous attempt after its terminal status. |
| Pause result names `/goal clear` | Explains how the user can discard instead of merely pause a goal. |
| Refusals start `Nothing was changed.` | Distinguishes pre-effect refusal from uncertain execution failures. |
| Creation refusals say to continue the task, or name user clear/resume for an open goal | Gives a next action without another forbidden creation or silent replacement. |
| Early-block refusal names the required and current automatic turn count | Explains why continuation remains active; retries within one turn do not satisfy it. |
| Missing-blocker refusal names `blocked_reason` | Gives the repair before any state change. |
| No-goal feedback names the user's `/goal` | Gives a recovery path even when this Agent cannot create goals. |

## Source and tests

`core/tools/create_goal.py`, `core/tools/update_goal.py`;
`tests/core/tools/test_update_goal.py` covers production dispatch, creation,
authorization, aliases and blocker refusals. `test_chat_goal_session.py` covers
same-Run creation, user `/goal` with creation unselected or denied, and typed
and existing-prefix Compaction retention. `tests/core/automation/test_goals.py`
owns lifecycle; `tests/core/runtime/test_runtime_wiring.py` owns registration.
