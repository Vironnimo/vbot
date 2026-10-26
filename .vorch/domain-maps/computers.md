# Computers

`core/computers/` owns execution-target identity, availability, exact Session selection and bounded requests to native Desktop capabilities.

## Overview

`ComputerRegistry` in `computers.py` is the shared Runtime service. It keeps the server target and known Desktop installations, binds live connections to durable identities, resolves a Tool call's selected Computer, and correlates requests, results and events. It does not interpret native process ids, windows, screenshots or application state.

Capability owners keep their semantics: Terminal manages processes and output (`tools/terminal.md`); Computer Use manages target approval, screen references and input (`extensions/computer-use.md`). `server/_computer_socket.py` owns the native transport, and `desktop/execution.py` owns local consent, handler factories and the connection lifetime (`desktop.md`). Session persistence and operator changes of selection belong to `sessions/lists-and-metadata.md` and `server/session-agent-rpcs.md`.

Shared terms Computer, Device, Session and Tool live in `.vorch/GLOSSARY.md`.

## Identity and storage

- `server` is the local execution target, always online in the registry. Its native capability owners execute locally; `ComputerRegistry.call` routes only to Desktop connections. The server advertises Terminal everywhere and Computer Use on Windows.
- Desktop identities are `pc_` ids. A name is display metadata, not ownership; ids survive a network reconnect and server restart. A Desktop process supplies an `instance_id`, distinct from its durable Computer id; the registry also has a per-start `server_instance` so native clients can discard process state whose server owner has restarted.
- `_store.py::ComputerStore` owns the Generation 1 document `access/computers.json` (kind `computers`): a named collection keyed by id, with name/platform/capabilities, the admitting principal's actor and a SHA-256 registration-key hash. Registration is durable before the connection becomes visible. The registration key proves the installation: a different key is `computer_identity_conflict`, and an id or Device token alone cannot replace another installation. With the right key a changed sign-in takes the registration over (for example `open` to a paired Device), except that an `open` connection cannot take over one registered with a sign-in (`computer_sign_in_required`). A new or renamed registration needs a name that no other registration uses, compared case-insensitively after whitespace collapsing (`computer_name_taken`), and never `Server` (`computer_name_reserved`); a stored name stays usable when unchanged. At most `MAX_COMPUTERS` (32) Desktop registrations exist (`computer_limit_reached`); a registered one still reconnects at the limit (user decisions 2026-10-26).
- The registration key lives in Desktop's protected per-server credentials (`desktop.md`). It is separate from the credential that admits the socket through the access guard. This distinction also applies to `local`/`lan` requests admitted as `open`.
- The store uses the shared validated atomic writer with owner-only permissions and unknown-field preservation. Invalid or unreadable stored registrations disable registration without overwriting the file; the local server remains available. `doctor config` validates it. It is outside data snapshots and configuration backups, so restore cannot roll Computer ownership back (`json-documents.md`). Stored Computers load offline until they register again.

## Selection and routing

`policy.py::normalize_execution` reads the Session's `execution` metadata: default `computer_id` plus `allowed_computer_ids`. Absence means server-only. An explicit empty allowed set stays empty, and an invalid explicit target never becomes the server. The policy stores identity and authority, not a live connection; Session owners preserve unknown metadata when writing it.

`resolve(context, requested_id, capability)` uses the Session default when the call omits a Computer. It accepts an exact registered id or one unambiguous case-insensitive full name, then requires that id in the Session's allowed set, an online connection, and the requested shared capability. It never fuzzy-matches or selects a different Computer after refusal. Agents cannot grant themselves another Computer by passing its id.

Capability consumers resolve before execution and retain the selected id. State tied to a native process or desktop also retains its `instance_id`: later `call` operations can require that same instance. A reconnect of the same app can resume capability-specific reads; a replacement instance refuses the old state before sending. Capability owners decide how to reconcile their own output and resources.

`call` runs on the serving Event Loop; `call_sync` is for native workers and refuses that loop. Requests have bounded frames, queues, pending calls and wait time. A pre-send refusal says nothing was sent. Timeout or connection loss leaves effects unknown; pending mutations are never replayed. Cancellation removes a pending request and the socket skips queued requests whose caller has gone; it does not roll back work the Desktop already accepted. A link replaced by a newer connection of the same installation is closed normally (1000).

## Native connection

`/ws/computers` is a dedicated bidirectional command connection, separate from the server-push-only `/ws`. The existing access guard admits it and closes Device-owned connections on revocation/expiry. The socket additionally requires an admitted principal and rejects browser Origin headers. Protocol 1 starts with Desktop `hello`, ownership validation and server `welcome`; `call`/`result` correlate execution, while `event` carries capability-owned changes. `computers.list` returns public descriptions only, and connection changes publish `resource_changed(kind="computers")`. `computers.remove {id}` (`server/rpc/computer_methods.py`, operator principal only, WebUI Settings -> Computers) calls `ComputerRegistry.forget`: it refuses `server` and a connected computer, deletes the stored registration and its offline entry, and emits the registry's own `removed` event. Sessions keep the id, which stops resolving; remote terminals pinned to it end. The same installation can register again later.

Only the currently connected instance for an id receives requests. Replacing its connection fails old pending requests and closes the old sender. Late results belong only to their original connection. Native capability events must name a capability that connection advertised; the registry supplies the trusted Computer and instance identity, and reserves `connected`/`disconnected`/`removed` for its own lifecycle.

The running Desktop app hosts native handlers, including while minimized; it starts no independent daemon or local Runtime. Native Terminal primitives and Computer Use targets are reusable imports, while Agents, Session state, permission decisions and orchestration stay on the server. Disconnect and close are separate handler hooks: Terminal preserves processes across a network drop, Computer Use stops/releases input, and ending the Desktop scope closes its native resources. Desktop owns per-server consent and close confirmation (`desktop.md`).

Adding a native capability extends Desktop's consent/factory catalog and supplies a server-side consumer plus its capability protocol. The registry and socket route capability names and operation payloads without Terminal or Computer Use branches. Extensions reach this same service through API 21 `host.computers`; they do not need Runtime or transport access (`extensions/host.md`).

## Lifecycle and evidence

Runtime constructs the registry before TerminalManager, injects it there and into Extension hosts, and closes it after TerminalManager so active terminal cleanup still has its connection (`runtime/lifecycle-steps.md`). Registry mutation and publication stay on the serving Event Loop; durable registration writes run on a bounded worker.

The behavior contracts live in `tests/core/computers/test_computers.py` (exact policy, durable ownership, reconnect fencing, bounded request lifetime and no replay), `tests/server/test_computer_socket.py` (admission, correlation, cancellation and disconnect), and the native owners' Desktop/Terminal/Computer Use tests.

## Agent-facing text

This domain owns the selection and request failure messages in `core/computers/computers.py` that native capability Tools surface. Before you change, add or delete a sentence of them, read `computers/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing Computer selection or remote-request failure wording -> `computers/agent-facing-text.md`
- Changing Desktop sharing consent, connection credentials or native handler lifetime -> `desktop.md`
- Changing native Terminal ownership, reconnect output or process identity -> `tools/terminal.md`
- Changing Computer Use targets, approvals, screen references or input -> `extensions/computer-use.md`
- Changing persisted Session execution selection, operator updates or delegation -> `sessions/lists-and-metadata.md`, `server/session-agent-rpcs.md` and `subagents/delegation.md`
- Changing access-guard admission, principal identity or Device revocation -> `access.md`
