# Desktop computer sharing

`desktop/execution.py` owns the running Desktop's local capability grants and native connection. The user chose this lifetime explicitly: a minimized app remains available; quitting ends its local work, with a warning for live Terminals. No separate daemon is installed. Server registration, Session target choice and authorization belong to `computers.md`.

## Ownership

- `ComputerSharing` owns one async loop on a native thread, independent of the loaded WebUI document. `main.py` starts it after the first connection attempt, follows `ConnectionController`'s selected server, and closes it when the window ends.
- `TerminalCapability` (`desktop/terminal_execution.py`) owns native PTYs and their output retention. Server TerminalManager continues owning the operator/Agent catalog, screen rendering, attachment and completion behavior (`tools/terminal.md`).
- `ComputerExecution` (`desktop/computer_execution.py`) owns the native desktop worker and emergency stop. The Computer Use Extension owns Agent grants, screenshot references and action interpretation (`extensions/computer-use.md`).
- A capability factory receives an event sink and returns a handler with async `execute`, `disconnected` and `close`; a handler can expose readiness and a `read_only_operations` set. Those operations remain admitted during native confirmation/restart fences so observing retained state survives a cancelled dialog. Terminal declares `read` and `status`. Factories load lazily. New capabilities share transport and lifecycle, not another Tool dispatcher or local Agent Runtime.

## Local consent and identity

The `computer_sharing` section of Desktop settings stores the display name and separate Terminal/Computer Use grants per normalized server origin; both default off. This state belongs to the Desktop config directory, never the server data directory. The bridge exposes only public status through `getComputerSharing`/`setComputerSharing`; `PagePermissions.computer_sharing` requires that the calling loaded page still belongs to the selected server before and after native consent. Enabling a previously disabled capability on a remote server needs the native dialog; the installation's own server already has this computer's rights. The mutation rechecks the selected server after consent.

`DeviceTokenStore.computer_identity` keeps a separate registration object inside that origin's record in `device-tokens.json`: a stable computer id and a secret protected with the existing Windows DPAPI or POSIX owner-only storage. Its key never reaches page JavaScript, status, events or logs. Forgetting the server removes its identity too; registering again allocates a new computer id. The prior Session target therefore remains unavailable rather than silently referring to the new registration.

## Connection and execution

The native client connects outbound to `/ws/computers`, with `ServerAccess.header`'s exact-origin credential policy, the Desktop TLS context, no environment proxies, a bounded 16 MiB frame and a protocol-versioned hello. It offers only enabled, ready capabilities. The server's welcome identifies the server process and the registered computer incarnation. Calls are bounded and concurrent; capability owners serialize operations where their semantics require it. A call id already received is refused without repeating its effect. Native events share the same connection. A server `error` frame (such as `computer_identity_conflict`) becomes the status `error_code`, not a protocol error. Reconnect waits double from 1 s up to 30 s and start again at 1 s after a link that was welcomed. A window reconnect to the same origin keeps a connected link unless the origin's credential changed (a completed pairing).

A network interruption keeps the same native scope and PTYs. The Computer Use handler stops local input and releases held keys/buttons. Admitted calls settle; their replies are dropped when their originating connection is gone, never delivered to a replacement socket and never replayed as mutations. A reconnected server can read retained Terminal output. A changed server process closes the previous native resources and registers a fresh incarnation before accepting new work. Selecting a different server also closes the old scope; resources and grants never transfer to another origin. `ConnectionController` enters the native server-change guard only after the new target has answered and sign-in succeeded. Running or starting Terminals require a native warning naming the target. A cancelled switch, failed probe/sign-in or settings-write failure preserves the previous scope and releases the admission fence; initial launch and same-origin reconnect do not prompt.

The public status push `vbot-desktop-computer-sharing` is a single coalesced snapshot through `PageEventDispatcher`. Browser refreshes and navigation do not own the native connection.

## Closing and updating

Closing first fences new mutations, then warns if native Terminals are running or starting. Terminal reads and status remain available during the dialog. A declined warning lifts the fence. Accepted close shuts down the native handlers before the Desktop exits. Turning Terminal sharing off while native Terminals run or start asks a native warning first (the bridge's `confirm`, user decision 2026-10-26); a declined or unavailable warning answers `declined` and keeps it shared. Disabling a capability closes its handler. No change silently redirects operations to the server computer.

Automatic package restarts wait indefinitely for local Terminals. Every admitted restart also fences native requests before the successor starts, so a new Terminal cannot slip between the idle check and the handoff. Explicit restart refuses while Terminals remain; the user can stop them first. A failed handoff lifts the fence so the existing app remains usable (`desktop/update-restart.md`).

## Agent-facing text

These transport refusals can be propagated through Terminal or Computer Use. They carry no Tool definitions; capability-specific results and guidance belong to those Tools.

| Text | Reason |
| --- | --- |
| `This request was already received; no action was repeated` | A duplicate request is not another input or process launch; prevents repeating a possibly completed action after reconnect. |
| `The computer has too many pending operations; nothing was started` | Distinguishes refused admission from an action whose outcome is unknown. |
| `This capability is not shared; nothing was done` | Prevents treating a revoked local grant as a transport retry or assuming local effects occurred. |
| `The Desktop is closing, restarting or switching servers; nothing was done` | Explains refusal during the native admission fence without encouraging alternate-machine execution. |
| `The operation and its parameters are required; nothing was done` | Refuses a malformed internal request before the capability sees it. |
| `The computer operation failed; its effects are unknown.` | Unexpected native failure cannot imply an action was not executed. |
| `Check the current state before repeating it.` | Prevents a second launch or input after an uncertain operation. |

## Tests

`tests/desktop/test_execution.py` covers per-origin grants/identities, call admission and duplication, app close, same-server reconnect, changed server identity, confirmed/cancelled/failed server switches and stale reply isolation using in-memory sockets. `test_bridge.py` covers native consent and navigation races; `test_restart.py` the unbounded Terminal hold; `test_page_events.py` coalescing. Native capability contracts have their own tests; none of these tests captures the user's desktop or launches their programs.
