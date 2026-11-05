# Session Goals

Goals started with the user's `/goal` or the Agent's configurable `create_goal`,
and the automatic Runs that pursue them, owned by
`core/automation/goals.py::GoalService`. The Session-granted `update_goal`
stops continuation (`tools/update_goal.md`). Design record and research: the git-ignored plan
`.vorch/plans/session-goals.md` (2026-10-15).

## State

One goal per Session, in open Session metadata `goal`
(`SESSION_GOAL_META_KEY`): `objective`, `status`, `pause_reason`, `turns`
(automatic Runs since the last set, resume or reactivation), `max_turns`,
`status_run_id` (the Run whose `update_goal` set the status), `created_at`,
`updated_at`. Forks strip it; an Agent Takeover keeps it, paused; `/handoff`
does not carry it. Session summaries (`session.get`, `session.list`) expose
`objective`, `status`, `pause_reason`, `turns` and `max_turns` as `goal`.
An Agent-reported block stores its concrete `blocked_reason`; completing or
resuming clears it. A new `blocked` status requires three automatic turns and
a non-empty reason. The Agent judges whether the same condition persisted;
the kernel checks the turn count only.

`create_from_agent` creates an active goal with the default 30-turn limit inside
the exact current Run with direct user input, starting no second Run and
refusing replacement of an open goal. Chat supplies the host-authored
`direct_user_input` execution fact for real User/Channel input and delivered
human steering; the Run's original kind/internal flag stays unchanged. The Tool refuses Sub-Agent Sessions
and group conversations. Both creation paths share the existing lifecycle and
goal rows. Disabling `create_goal` does not affect a user-started goal or its
`update_goal` Session Grant.
Agent-created goal rows are recorded through a fresh Session handle outside
the Run; after that write, the changed notification carries `history=True` so
live accessors reload the new divider as well as the goal bar.

| Status | Meaning | Leaves it |
|---|---|---|
| `active` | Automatic Runs continue. | `update_goal`, a kernel pause, `/goal pause`, `/goal clear` |
| `blocked` | The Agent needs the user. | Explicit `/goal resume`, or the next completed `user` or `channel` Run that is not internal and is not the Run that blocked it: `active` with `turns` 0 |
| `paused` | Stopped until `/goal resume`; `pause_reason` says why. | `/goal resume` (`turns` 0) |
| `complete` | The Agent reported it done. | `/goal resume`, a new `/goal <objective>` |

Pause reasons: `user` (`/goal pause`), `agent` (`update_goal` `paused`), and the
kernel's `stop`, `run_failed`, `limit`, `restart`, `takeover`.
A kernel pause writes one note (`The goal was paused because <reason>. The user
can resume it with /goal resume.`); `user` and `agent` write none, since the one
who paused already knows. Background results never resume a paused or blocked
goal.

## Goal rows

Every goal change is a row of the conversation, so the user can follow a goal
in the History (user requirement 2026-10-17: everything the user writes and
every goal status stays visible). A row is a note carrying a `GoalEvent`
(display records: `chat.md` -> Invariants and conventions): `event` is `set` (with `objective`
and `max_turns`), `turn` (`turn`, `max_turns`), `paused` (`reason`;
`max_turns` for `limit`), `resumed`, `reactivated`, `blocked`, `complete` or
`cleared`. Its text is the Model-facing note, empty when the Agent needs none
(a turn, a user pause, a clear, the Agent's own status). The Model never sees
the display record.

- `set` and `resumed` are the context note of the Run they start, so they sit
  in that Run before its first output; `turn` is the context note of each
  automatic Run. The Run emits `goal_event_recorded` for live clients.
- The other changes, and a set or resume that starts no Run, are written
  outside any Run through `_record` under the Session's write lock.
  `complete`, `blocked` and `paused` with `agent` follow the Run that set them,
  only when that Run's `update_goal` did (`status_run_id`).
- A row outside a Run reaches clients as `resource_changed(sessions)` with
  `history: "appended"` (the changed callback's `history` flag); the WebUI then
  rereads the displayed Session's History.

## Continuation

`install()` registers a `ChatRunManager` start callback and a completion
observer on every Run. After the Run's terminal commit, a spawned decision runs:

1. No goal: nothing. `cancelled` with a reason other than `shutdown`: pause
   `stop`. `failed` or `interrupted`: pause `run_failed`.
2. `blocked`: reactivate only for a reactivating Run (table above).
3. Not `active`: nothing.
4. `turns >= max_turns`: pause `limit`.
5. Queued input for the Session: nothing; that Run's end decides again.
6. Pending wakes from `SessionActivity.pending_wakes` (`activity.md`), plus
   `CommandManager.has_pending_delivery`: wait. This includes transitive
   Sub-Agent work, committed Parent answers after a recipient change, forwarding
   gaps and held completions; unlike UI activity, it excludes idle settle periods.
   Goals deliberately still count service commands. An unavailable wake check
   also defers, logging degradation once and recovery when reads succeed again.
   The result's Run decides again; the fallback continues after
   `GOAL_WAIT_FALLBACK_SECONDS` (10 minutes), including when the check stayed
   unavailable or a command such as a dev server never ends.
7. Otherwise count the turn and start an internal `system` Run through
   `ChatLoop.start_run` with the Session's latest reply surface, the input
   `goal_continuation_text`: the quoted objective, automatic turn/count limit
   and work/evidence instructions, plus `GOAL_LAST_TURN_TEXT` at the limit,
   and the `turn` row as its context note. A Channel surface relays the Run through `set_run_relay`. A start that
   loses the Session to another Run uncounts the turn; a failed start pauses
   `run_failed`.

There is no progress check: a turn without a Tool call continues the goal, since
goals such as writing or counting need none (live eval 2026-10-16: a counting
goal paused after its first automatic turn under the former `no_progress` rule;
user decision to drop it). Waiting for running work prevents idle turns, and the
limit bounds the rest. A Runtime start pauses every `active`
goal with `restart` before any Run starts (`pause_after_restart`), so no goal
continues unattended after a crash or update.

## Commands

`/goal` is a Built-in of `CommandDispatcher` that delegates to
`GoalService.execute_command` (`chat/commands.md`). It is unavailable in group
conversations (user decision 2026-10-15).

- `/goal <objective> [--max N]` sets or replaces the goal (`--max` 1 to 200,
  default 30, user decision 2026-10-15) and starts the first Run as a visible
  `user` Run: its message is the user's typed command
  (`CommandExecutionContext.command_text`, with the sender), its context note
  the goal-set note with the `set` row. Refused while a Run of the Session is
  active: `Stop the current turn first, then set the goal.` When another Run
  wins the Session between that check and the start, the goal stays set, the
  note and row are written on their own, and the reply is `Goal set. It starts
  after the current turn.` It can create a new Session.
- `/goal` reports `Goal (<status or pause reason>): <objective>` and
  `Automatic turns: N of M.`
- `/goal pause`, `/goal resume`, `/goal clear` are immediate and each writes
  its row. `resume` works from `paused`, `blocked` and `complete`; when the Session is
  idle it starts a visible `user` Run like a set, with `GOAL_RESUMED_TEXT` as
  the context note, otherwise it writes the row alone and the current Run
  continues the goal when it ends.
- `/stop` always pauses an open goal with `stop`, also without an active Run.
  Live goal transitions and automatic Run admission share a per-Session lock;
  preparation outside it cannot start a Run after a pause or clear. A Stop
  that races admission also cancels the automatic Run admitted before it acquired
  the lock, identified by that admission; a queued user Run that takes over the
  Session stays untouched. No automatic Run escapes the gap between Stop's first
  lookup and its pause. History appends happen after releasing this lock because
  an `update_goal` Tool already holds the Session append lock when it changes goal
  metadata; the two locks must never be acquired in the reverse order.

## Chat and Compaction

Chat grants `update_goal` as a Session Grant while the goal is `active`,
`blocked` or `paused` (`has_open_goal`). While it is open, the Compaction
finalizer keeps the latest `GoalEvent("set")` note verbatim after the summary;
`SESSION_GOAL_NOTE_PREFIX` also identifies untyped goal notes already in
History. Pause notes and per-turn inputs are not retained. Chat refreshes the
open-goal grant at each Model-request boundary, so a goal created inside a Run
offers `update_goal` in that Run through ordinary Tool-change announcements
without replacing the pinned Tool list.

## WebUI

`ComposerGoal.svelte` shows the goal above the composer from the Session's
`session.get` row: status or pause reason, objective, turns, and buttons that
send `/goal pause|resume|clear` through the ordinary send path. Every goal change
publishes `resource_changed(sessions)` for the Session through
`add_changed_callback`, which rereads the row. Goal rows render as divider-style
status lines, inside a Run or between Runs (`webui/chat.md`).

## Deviations from the plan

- No `RunKind.GOAL`: automatic Runs are `system`, Runs from set and resume are
  visible `user` Runs (they were internal until 2026-10-17, which hid the
  user's `/goal` message).
- No `no_progress` pause (Continuation).
- The goal-set note is found by its display record or existing prefix, not by a note id in metadata.
- The indicator sits above the composer, not in the chat header.

## Agent-facing text

The kernel notes are English constants in `goals.py`; the objective is the only
user text, quoted with `quote_external_value`. What each status means lives only
in the `update_goal` definition; reasons for those sentences:
`tools/update_goal.md`.

| Text | Reason |
|---|---|
| `Session goal: {objective}` | Identifies user- and Agent-started goals without falsely attributing a `/goal` command; the objective is JSON-quoted. |
| `When your turn ends while this goal is active, vBot starts your next turn automatically.` | Without it Agents ask "Shall I continue?" or wrap up each turn as finished (Goose, Hermes). |
| `Use update_goal to mark the goal complete or blocked.` | Names terminal control without inviting an unsolicited pause. |
| `Goal: ...` / `Automatic turn: N/M` | Each automatic Run receives its objective and allowance position rather than reconstructing them. |
| `Continue work on this goal in the same Session.` | Keeps automatic input focused on unfinished work; DSH-derived text approved 2026-11-05. |
| `Ground your next step in the current state of the work and relevant Tool results; earlier narration can be outdated.` | Prevents stale narration from replacing evidence without forcing workspace reads for non-file tasks. |
| `Make concrete progress and verify the result.` | Directs continuation toward work and checking rather than another recap. |
| `Before claiming completion, gather evidence that the whole objective is achieved.` | Requires evidence for the whole objective rather than only the last step. |
| `Then mark the goal complete with update_goal.` | Connects verified completion with the operation that stops automatic Runs. |
| `Otherwise, leave it active so the remaining work can continue in the next turn.` | Keeps unfinished useful work inside continuation. |
| `Apply the status rules in update_goal when deciding whether to report a blocker.` | Leaves the detailed status policy in its owning definition. |
| `This is the last automatic turn; after it, the goal pauses until the user resumes it.` | Announces the impending stop before work begins. |
| `Take the next useful step, then tell the user what is done and what remains.` | Ends the allowance with useful work and an account of the remainder. |
| `Reaching the turn limit does not make the goal complete.` | Prevents allowance exhaustion from substituting for success. |
| `The user resumed the goal. Continue with the next step toward it.` | Context note before the user's `/goal resume`; without it the Agent cannot tell that the command resumed work and what to do. |
| `The goal was paused because <reason>. The user can resume it with /goal resume.` | When the user later asks why work stopped, the Agent can only answer from it. Written as a fact, not a state that can change after the note. |

## Source and tests

- `core/automation/goals.py`; `tests/core/automation/test_goals.py` (continuation,
  every pause reason, blocked reactivation, waiting and unavailable-work fallback, commands, restart, the
  rows each change writes).
- Grant and Compaction retention: `core/chat/_run_state.py`,
  `core/chat/_request_builder.py`, `core/chat/_scratch_context.py`;
  `tests/core/chat/test_chat_goal_session.py`.
- Wiring: `core/runtime/_bootstrap_services.py` (`_goal_work_pending` delegates to Activity and adds service commands; `tests/core/runtime/test_runtime_wiring.py`), shutdown step
  `goals`, server bridge in `server/_app_lifecycle.py`.
- WebUI: `webui/src/components/chat/ComposerGoal.svelte`,
  `ChatView.sessionSettings.test.js`; rows: `goalEventLabel`
  (`chatTimelinePresentation/messages.js`), `ChatTimeline.runs.test.js`.
