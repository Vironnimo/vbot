# Computer Agent-facing Text

Reasons for selection and execution failures from `core/computers/computers.py`, surfaced by Terminal and Computer Use. Native operation-specific failures belong to those owners. These rows describe contract-derived failure modes, not observed Model Evals; the regression evidence is `tests/core/computers/test_computers.py` and `tests/server/test_computer_socket.py`.

| Text | Reason |
|---|---|
| `Computer {computer_id!r} is not registered.` | An explicit missing target must not silently resolve to the server or a similar Computer. Names the value the caller must correct. |
| `Nothing was started or sent.` | Distinguishes selection failure before execution from an operation whose effects need inspection. |
| `Choose a registered computer in this Session's settings.` | Gives the operator recovery path instead of encouraging guessed ids or broader grants from the Agent. |
| `No computer was selected; nothing was sent.` | An invalid explicit selection must not become an omitted default; the caller knows the failure preceded execution. |
| `Computer {info.name!r} is not enabled for this Session.` | Separates a registered Computer from one this Session is allowed to use. |
| `Nothing was sent.` | Reused for policy, availability and request-admission failures; it permits correction without falsely implying a partially applied mutation. |
| `The user can enable it in the Session's computer settings.` | The Agent's target argument cannot extend authority; identifies who can grant the target and where. |
| `Computer {info.name!r} is offline.` | Keeps loss of availability distinct from a bad target or missing permission and prevents server fallback. |
| `Reconnect its Desktop app to this server, then try again.` | Gives the prerequisite for an offline target without proposing that the Agent start a second daemon or execute elsewhere. |
| `Computer {info.name!r} does not share {capability}.` | A connected Computer can share one capability without another; changing the target spelling cannot fix that consent state. |
| `Enable that function in its Desktop app.` | Routes capability consent to the machine whose native resources would be controlled. |
| `The computer connection ended after the request was sent; its effects are unknown.` | A dropped connection cannot prove that input or a process start failed; prevents repeating an uncertain mutation. The registry conservatively treats admitted pending requests this way. |
| `Reconnect and inspect the current state before repeating it.` | Recovery must first determine the native state; transport reconnection alone is not permission to replay. |
| `Computer operation failed; inspect its state before retrying.` | A malformed or incomplete remote error still needs a recovery step and cannot establish that no effect occurred. |
| `The Desktop app restarted.` | Explains why a previously valid process or desktop reference cannot address the replacement instance. |
| `Nothing was sent to the replacement instance.` | Makes the instance fence's outcome explicit so the Agent does not treat a replacement process as the old one. |
| `The computer is not connected.` | Covers a connection disappearing between selection and admission without misreporting a native execution failure. |
| `The computer has too many pending operations.` | Names bounded admission as the failure, so the caller does not change credentials or select another machine. |
| `Nothing was sent; try again after they finish.` | The rejected request needs no effect check; wait for admitted work to free capacity. |
| `The computer request is too large.` | Identifies a request-bound failure rather than target availability. |
| `Nothing was sent; split it into smaller calls.` | Gives a viable recovery for oversized frames without implying that part of the request ran. |
| `The computer is busy.` | Names a full outgoing queue as temporary backpressure. |
| `Nothing was sent; try again after pending work finishes.` | Queued work must drain before another admission attempt; the refused operation itself has no uncertain effects. |
| `The computer did not answer in time; the request's effects are unknown.` | A deadline stops waiting, not native execution; prevents duplicate terminal input or UI actions after a slow reply. |
| `Inspect its state before repeating it.` | A timeout cannot safely choose a replay; callers must recover through their capability's state reads. |
| `No Desktop computer is connected.` | A worker-side call without a serving connection cannot route execution; avoids suggesting that the operation ran locally. |
| `The Desktop app restarted; the earlier process is unavailable.` | A waiter must stop when the native instance changes instead of waiting for an old process to reappear in a new app. |
| `The server's computer connections have closed.` | Ends availability waits on Runtime shutdown; reconnecting to this retired registry cannot recover them. |

Registration-only errors (`computer_protocol`, `computer_store_unavailable`, `computer_identity_conflict`, `computer_registration_failed`, `computers_closed`) report to the Desktop connection, before any capability request. They describe transport/ownership state; they do not grant Session authority or advise replaying an operation.
