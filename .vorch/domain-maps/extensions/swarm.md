# Swarm

Read for changes to the bundled Swarm Extension. The generic Extension contracts
remain in `extensions.md`; Swarm policy belongs under `resources/extensions/swarm/`.

## Owners

- `extension.py` owns registration, management operations, the three Tool handlers,
  and coordination with owner-bound temporary execution groups.
- `store.py` owns the SQLite profile, Board, audience, delivery, lifecycle and audit
  transactions. It receives canonical receipt and terminal-proof lookups; it must
  not open the Session database directly.
- `agent_text.py` owns the scoped Tool definitions and reviewed runtime wording.
- `ui/SwarmPage.svelte` and `ui/ProfileEditor.svelte` own the domain page. The app
  shell and page bridge remain generic; built assets live in generated `web/`.

## Invariants that affect changes

Profiles are versioned, revision-checked starting configurations. A started Swarm
retains its prompt and configuration snapshot. The formation is fixed; participant
Sessions persist across explicit Resume. Temporary execution ownership belongs to
`core/agents/temporary.py`, not to an Identity Agent or a parent Session.

Board posts are immutable and public within one Swarm. Audience snapshots survive
later join/leave changes. A public ping takes precedence over discussion/main
routing for that recipient, without creating duplicate deliveries. A mutation's
request id is payload-bound; reusing it with changed content is a conflict.

Preparing a batch is not delivery. Only a matching canonical Session receipt
acknowledges its contents. Tool batches are acknowledged after their complete
carrier is saved. Delivery mode and idle wake permission are independent. Read
pages are bounded and cursor scope includes the query and page size.

Wait, blocked, failed, cancelled and done are distinct. Done first records an
intent, then reserves completion under the same transaction boundary as Board
posts. Unread messages and owned work prevent completion. Only exact canonical
successful Run evidence finalizes the participant; an ordinary final answer does
not. Whole-group completion additionally requires a closed, drained execution
group and every participant done.

Participant transitions recompute the open Swarm's aggregate state: unfinished
idle/waiting peers yield waiting, blocked/failed peers require attention, and
active work stays running. The page's active Run indicator comes from exact
canonical Run inspection, not a retained lifecycle Run id.

The Store's integer lifecycle epoch and the temporary facade's opaque admission
epoch are different identities and are linked explicitly. Stop closes admission;
retained records are not deleted. Startup recovery marks unfinished execution
interrupted and never admits work automatically. Stale callbacks cannot reuse an
old registration to start or mutate new execution.

Explicit Resume may continue inactive unfinished peers in an open epoch while
other peers run. A closed attempt opens a fresh epoch. Preparation failure closes
that newly opened epoch so a later Resume can retry; existing Sessions and the
initial-input receipt remain authoritative. Resume is rejected while initial
preparation or Stop is still in progress. Background wake/completion failures
retain pending delivery and expose needs_attention with ids-only diagnostics.

## Verification routes

- Board/profile/policy transactions, races, receipt recovery and lifecycle:
  `tests/resources/extensions/test_swarm_store.py`.
- Registered private Tool behavior and management boundaries:
  `tests/resources/extensions/test_swarm_board.py`.
- Actual Chat participants, terminal proofs and Stop/Resume:
  `tests/resources/extensions/test_swarm_lifecycle.py`.
- Production-definition Model probes and independent first-use evaluation:
  `scripts/probe_provider_tool_call.py` (`swarm_tool` scenario), with probe tests
  under `tests/scripts/test_probe_provider_tool_call.py`.
- Rendered business controls: `webui/src/components/__tests__/SwarmPage.test.js`;
  generic iframe isolation is tested by ExtensionPage and server asset tests.

Use the Sessions, Runs, Chat and Statistics maps before changing their owning
contracts. Swarm must consume their canonical history and usage instead of
maintaining a second transcript or authoritative usage counter.
