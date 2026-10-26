# Session Coordination

The `session` Tool lets ordinary Sessions find, inspect and message peers in the
same non-null Working Project. This is peer communication, not Sub-Agent creation,
ownership or automatic final-answer forwarding.

## Ownership

`core/tools/session.py` owns the flat Tool contract, argument repair, send admission
policy and call display. `core/chat/session_peers.py` owns discovery, bounded reads
and event-driven waiting over canonical Sessions, Run and Activity APIs. Chat's
`submit_session_message` owns durable delivery through accepted input and the
existing Run Queue. There is no second scheduler or mailbox database.

## Contracts

- Project identity comes from the Session's Working Project, never a directory
  match or loaded Project Context. Every operation rechecks the live source and
  destination; references bind the complete Session address and generation so
  reused Session ids cannot redirect a send.
- `list` returns bounded peers, their activity and recent progress. `read` returns
  the latest human task and a bounded conversation page; it excludes Tool output
  and Reasoning. Pagination and wait cursors are opaque values copied from results.
- `send` returns after durable acceptance, not after the recipient reads or acts.
  Its baseline cursor is captured before admission so a fast reply is not lost.
  A replay of the same Tool invocation uses the same accepted-input receipt;
  identity includes the Run's iteration and call position because Providers can
  reuse call ids. Conflicting payloads fail instead of changing an accepted message.
- A received message is visible and carries a Session sender, rendered as quoted
  peer content for the Model. It reaches a running Run at its next Model boundary
  without aborting a Tool, or wakes an idle Session. Answers to the user keep their
  existing destination; replying to a peer is a separate `send` call.
- `wait` observes changes after its baseline, not an already-seen completion. It
  also returns for input to its caller so a Tool wait cannot hold up that input.
  It waits for at most 60 seconds and holds no shared Tool execution slot.
- Restricted readers follow Session Recall's own-Agent and inherited-tree limits
  in addition to the Project boundary (`session_search.md`). A sender with
  Permission Rules or an execution-time Tool ceiling cannot use an independent
  Session to evade those limits; `send` refuses it before acceptance. Sub-Agent
  delegation is the existing path that carries a caller's ceilings.
- Accepted peer input survives restart through the same receipt recovery as
  ordinary accepted input; interrupted Runs themselves are not replayed. Details
  and provenance storage: `chat/run-execution.md`, `sessions/runs.md`.
- Messages are advisory coordination. They do not reserve files, branches or a
  merge operation; repository mutual exclusion belongs to the operation's owner.

## Discovery and observation

References encode the canonical owner address, Session generation and history
continuity id as opaque `session:` values. They identify a target and grant no
authority: every action rechecks the caller's live Working Project and readable
scope. Self, hidden Recall sources, Ephemeral Sessions, temporary bindings and
built-in Live/Librarian Sessions are excluded. Restricted readers use the same
own-Agent and inherited-tree policy as Session Recall.

`list` places the current Activity/Run set before idle Sessions, then pages the
canonical Project list with a bounded scan. Its progress omits incoming peer
messages and labels the source role so a human task is not mistaken for Assistant
progress. `read` carries the latest human task independently of its page, preserves
peer-message attribution and bounds message text to 2,000 characters. These reads
use Sessions' generation-checked conversation projection (`sessions/history-api.md`)
without taking another Session's write lease: reciprocal Tools must still finish
while Chat holds each caller's own write lease.

`next_cursor` continues list pagination or reads older conversation; `cursor`
records the target's current follow baseline for `wait`. Wait subscribes to Run,
Activity and accepted queued-input events, then rechecks canonical facts. Tool
progress alone and incoming peer messages are not replies. Wait returns at most
20 new messages and advances its cursor only over returned messages when more
remain; an already-delivered completion is not repeated. Cancellation and return
remove all observers. An accepted message queued to the caller releases its wait
before that message must be appended by Chat, preventing two peers from holding
each other's input behind simultaneous waits.

## Agent-facing text

| Text | Reason |
|---|---|
| `Coordinate with other Sessions in your Working Project.` | Distinguishes the peer Tool from delegation and historical recall, and identifies its scope. |
| `Messages are visible to the user and wake idle recipients.` | Sending has visible and execution effects, which the caller needs before choosing it. |
| `read shows the task and recent conversation.` | Explains the bounded coordination view rather than promising a full transcript. |
| `send delivers at the next Model step without interrupting Tools; replies use send too.` | Avoids treating send as cancellation or assuming normal user-directed final answers reach the peer. |
| `wait returns on new conversation, completion, or a message to you.` | Explains what wakes a blocked caller and permits progress without polling. |
| `Session reference from list or a received message.` | Uses the returned, generation-bound target rather than an ambiguous bare Session id. |
| Cursor omission and result-continuation instructions | Distinguish older-page retrieval from waiting for future work and prevent old completions being counted as replies. |
| `Message accepted for delivery.` and the following read/acted caveat | Acceptance proves durable receipt, not that the recipient consumed or complied with the message. |
| `Replies arrive as messages; continue other work, or use wait with this id and cursor.` | Gives the next step without requiring polling or serializing otherwise independent work. |
| Execution-limit refusal and the conditional Sub-Agent alternative | Independent peers cannot inherit the caller's ceiling; points to the existing bounded delegation path only when callable. |
| `Delivery was not confirmed.` and the read-before-resend instruction | A failure after submission cannot safely assert no effect or invite blind duplicate delivery. |
| `This Session has no Working Project. Start a Session in a Project to contact its Sessions.` | Gives the prerequisite and recovery without guessing Project identity from a directory. |
| `This is your Session. Use its existing conversation; use list to find another Session.` | Prevents treating peer communication as a second input route into the caller. |
| `Your Permission Rules do not allow reading this Session. Use list to find Sessions you can access.` | Preserves Recall's read boundary and provides an allowed discovery action. |
| `This Session is no longer reachable in your Working Project. Use list to find an available Session.` | Handles stale references, inaccessible generations and excluded targets without inviting a bare-id retry. |
| `This cursor does not match this operation or Session. Use the cursor returned by list or read for that operation.` | Rejects invented or cross-target cursors and explains where a valid value comes from. |
| History-changed refusal and the instruction to read again | An edit invalidates the previous follow position; a fresh read establishes a meaningful baseline. |
| Empty-list result and `Pass next_cursor to continue.` when present | Distinguishes a filtered page from the end of discovery, so a bounded scan does not look like global absence. |
| `A message reached your Session. Continue to receive it.` | Releases a waiting Tool so the ordinary Run boundary can consume its accepted input. |
| `More new messages are available. Use wait with this cursor to continue.` | Keeps bounded response pages complete without replaying or skipping messages. |
| `No new message or completion arrived before the wait ended.` | Describes a timeout without repeating old output or claiming the target finished. |

## Verification

Tool-call interpretation and send authority: `tests/core/tools/test_session.py`.
Canonical reads and wait behavior: `tests/core/sessions/test_conversation_page.py`
and `tests/core/chat/test_session_peers.py` (production Tool dispatch);
peer admission, steering, provenance and recovery extend Chat's accepted-input
tests. WebUI tests cover attribution on ordinary and steered conversation rows.
