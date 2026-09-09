# Swarm

Read for changes to the bundled Swarm Extension. The generic Extension contracts
remain in `extensions.md`; Swarm policy belongs under `resources/extensions/swarm/`.

## Owners

- `extension.py` owns registration, management operations, the three Tool handlers,
  and coordination with owner-bound temporary execution groups.
- `store.py` owns the SQLite profile, Board, audience, delivery, lifecycle and audit
  transactions. It receives canonical receipt lookups; it must
  not open the Session database directly.
- `agent_text.py` owns the scoped Tool definitions and reviewed runtime wording.
- `ui/SwarmPage.svelte` and `ui/ProfileEditor.svelte` own the domain page. The app
  shell and page bridge remain generic; built assets live in generated `web/`.
- The initial overview fetches retained profiles and Swarms only. Model, Tool,
  Skill and Project choices load when opening a profile editor, not on display
  context updates; failed editor loads leave the overview usable
  (`SwarmPage.test.js`).
- Profile editing uses the shared Model search/selection and effort helpers,
  the shared Secondary bar, topic tabs, a bounded scrollport, and a fixed save footer.
  Creation saves explicitly; saved profiles autosave and flush before navigation
  through the generic page bridge. Invalidations preserve the mounted draft.
  The System Prompt tab owns editable instructions, explicit block selection,
  Model-specific combined previews and event reminder switches. New profiles
  receive the complete default from `agent_text.py` through the editor catalog;
  Swarm no longer registers a separately appended orientation block.
  The visible value is saved as-is, including an intentionally empty value;
  existing profiles are not backfilled and runtime adds no fallback.
  Omitted effort uses the Provider default, not shared Agent defaults.
  All/None Tool actions materialize a selected policy through `toolAccess.js`.
  Tests: `SwarmPage.test.js` and `test_swarm_store.py`.

Profiles select every additional System Prompt block explicitly; Tool Call Style
and Skills start enabled, other blocks (including Runtime and Working Project)
start disabled, and newly registered blocks stay disabled. This selection controls
prompt text independently of working directory and Tool/Skill access. It is
persisted in temporary Agent bindings and each started Swarm's profile snapshot.
`profiles.preview` accepts an unsaved profile and formation row, using the host's
read-only prompt inspection with the same Project ceilings and Model Tool routing.
It returns rendered block details and separately transmitted Tool definitions;
draft changes invalidate the displayed preview. Evidence: `test_swarm_lifecycle.py`,
`test_runtime_extensions.py`, `test_prompts_layouts_overrides.py`, `SwarmPage.test.js`.

Delivery and explicit Resume guidance are individually
switchable in the snapshot. Their complete wording is inspectable in the editor.
Disabling guidance preserves Board payloads and lifecycle behavior; empty
continuation receipts add no Model-visible reminder. Existing Session history is
not rewritten. Evidence: `extension.py`, `test_swarm_store.py`,
`test_swarm_lifecycle.py`.

## Invariants that affect changes

Profiles are versioned, revision-checked starting configurations. A started Swarm
retains its prompt and configuration snapshot. The formation is fixed; participant
Sessions persist across explicit Resume. Temporary execution ownership belongs to
`core/agents/temporary.py`, not to an Identity Agent or a parent Session.
Saving without a command shortcut generates a unique name-based shortcut inside
the profile transaction; omission on an update retains the existing shortcut.
Explicit shortcuts remain validated and collision-checked (`store.py`).

Board posts are immutable and public within one Swarm. `swarm_board` can ping
participants on a discussion's opening message without joining those recipients;
creation, opening-message audience and the main-discussion announcement commit
atomically. Replies derive their discussion from the exact same-Swarm message
unless an explicit, matching discussion is supplied. Reads start with newest
posts, chronological within each page, and continuation moves to older posts.
The Board UI reverses each page for newest-first display and appends older pages
below it; this presentation does not change the Store or Tool read order.
Coverage: `test_swarm_board.py` and the production `swarm_tool` probe.

The default System Prompt and Board Tool description guide participants toward
the main discussion for shared conversation and coordination; additional discussions
are for several Agents working through a specific problem. New announcements carry
readable text, exact discussion/opening-post IDs and read/join guidance. Human Board
reads additionally resolve the discussion target from its creation request outcome,
so the page can render a localized navigation action without parsing message text.
Ordinary posts cannot acquire that action by copying an announcement's content.
Saved posts and profile snapshots are not rewritten. Evidence: `agent_text.py`,
`test_swarm_store.py`, and `SwarmPage.test.js`.

Audience snapshots survive later join/leave changes. A public ping takes precedence over discussion/main
routing for that recipient, without creating duplicate deliveries. A mutation's
request id is payload-bound; reusing it with changed content is a conflict.

Preparing a batch is not delivery. Only a matching canonical Session receipt
acknowledges its contents. Tool batches are acknowledged after their complete
carrier is saved. Delivery mode and idle wake permission are independent. A wake
always delivers actual pending Board content, including pull-mode messages on a
wake-enabled route; it never asks the Agent to fetch the first batch. Bounded
overflow remains available through Inbox. Read pages are bounded and cursors
cannot cross queries.

An already prepared wake batch remains replayable while its Run is active.
New posts on an idle-mode route wait until the participant is idle again or
explicitly reads Inbox; the retained wake boundary does not permit automatic
delivery during later Model requests of that Run. All-mode delivery still reaches
the next Model request while running (`test_swarm_lifecycle.py`).

Automatic delivery and Inbox entries retain the Swarm-wide post sequence, UTC
creation time, author identity, discussion id/title, reply target and explicit
ping recipients. The saved per-recipient route (`main`, `discussion`, `ping`)
explains why this participant received the post; Board reads expose it only for
participants in the original audience. Delivery batches are oldest-first, but
mixed route policies can deliver newer posts before older deferred ones; the
original sequence and timestamp remain unchanged. Tests: `test_swarm_inbox.py`
and `test_swarm_lifecycle.py`.

Inbox reads are nonblocking and consume only messages in their saved carrier;
continuations preserve an omitted limit. Empty results permit a normal final
reply; no Swarm Tool requests a Run end (`test_swarm_inbox.py`).

`swarm_state` is read-only, with optional cursor and limit. It returns a compact paged
roster, pending count and delivery/wake policy; cursors bind page size. Participants
cannot rename themselves. The Store shuffles a curated pool of 300 short given
names and callsigns once per new Swarm, assigning without replacement across
formation rows. Larger Swarms use numbered suffixes after the pool is exhausted.
Saved names survive request replay, restart and Resume; participant ids remain
the addressing contract (`test_swarm_store.py`). Progress, results and requests for help belong on the
Board, not in participant lifecycle fields. There is no participant-owned wait, blocked, finishing or done state,
completion reservation, summary store or automatic group completion.

Participant status is an execution projection: `idle`, `running`, `failed`,
`cancelled` or `interrupted`. A successful Run returns to idle and leaves the
same Session reachable. All-idle Swarms remain open without polling Models;
eligible new Board messages trigger Runs through the existing wake/receipt path.
Successful wake admission publishes a page invalidation so an open participant
Session can attach to the new Run before it finishes (`test_swarm_lifecycle.py`).
Messages are retained for every addressed participant, including failed or
cancelled peers. Automatic wakes apply to idle peers; explicit Resume recovers
failed/cancelled/interrupted peers. Stop and startup recovery mark only active
Runs cancelled/interrupted, retaining other participants' last outcomes.
Evidence: `test_swarm_store.py`, `test_swarm_board.py`, `test_swarm_lifecycle.py`.

Run callbacks recompute the open Swarm's aggregate state: any running peer keeps
it running, unsuccessful inactive peers require attention, and all-idle peers
yield idle. Repeated start/wake acknowledgments cannot overwrite the same Run's
terminal outcome. The page's active Run indicator uses exact canonical Run
inspection, not the presence of a retained Run id.

The Store's integer lifecycle epoch and the temporary facade's opaque admission
epoch are different identities and are linked explicitly. Stop closes admission;
retained records are not deleted. Startup recovery marks unfinished execution
interrupted and never admits work automatically. Stale callbacks cannot reuse an
old registration to start or mutate new execution.

Explicit Resume may continue inactive peers in an open epoch while
other peers run. A closed attempt opens a fresh epoch. Preparation failure closes
that newly opened epoch so a later Resume can retry; existing Sessions and the
initial-input receipt remain authoritative. Resume is rejected while initial
preparation or Stop is still in progress. Background wake failures
retain pending delivery and expose needs_attention with ids-only diagnostics.

Start and Resume persist admitted Run results in their existing request receipt;
replaying that request returns the saved outcome without preparing Sessions or
admitting work again. A completed Stop returns its saved drain result, and an
unfinished Stop can drain only its original lifecycle epoch. The receipts survive
restart and later Resume attempts. An interrupted admission requires a new explicit
Resume request, not replay of the old Start/Resume request. Evidence:
`test_swarm_store.py`, `test_swarm_lifecycle.py`.

## Verification routes

- Board/profile/policy transactions, races, receipt recovery and lifecycle:
  `tests/resources/extensions/test_swarm_store.py`.
- Registered private Tool behavior and management boundaries:
  `tests/resources/extensions/test_swarm_board.py`.
- Actual Chat participants, terminal proofs and Stop/Resume:
  `tests/resources/extensions/test_swarm_lifecycle.py`.
- Production-definition Model probes and independent first-use evaluation:
  `scripts/probe_provider_tool_call.py` (`swarm_tool` scenario), with probe tests
  under `tests/scripts/test_probe_provider_tool_call.py`. The `unassisted` case
  supplies the goal without prescribing Tools; success requires receiving peer
  feedback, a later public contribution, and a normal final response. It evaluates
  coordination effects, not the semantic quality of the generated checklist.
  The probe purges cached Extension modules before loading its own checkout.
- Rendered business controls: `webui/src/components/__tests__/SwarmPage.test.js`;
  generic iframe isolation is tested by ExtensionPage and server asset tests.

Use the Sessions, Runs, Chat and Statistics maps before changing their owning
contracts. Swarm must consume their canonical history and usage instead of
maintaining a second transcript or authoritative usage counter.

The retained Swarm list projects a bounded first-line goal title from the stored
prompt. Profile selection opens the editor; New Swarm returns to the goal form.
Participant selection opens Activity and disposes the previous Run subscription;
late history/subscription replies cannot replace a newer participant selection.
Terminal Run events reload canonical history so non-streamed final output appears
without reopening Activity. Refresh and reconnect also reload the open History
and reattach an active Run when its id is unchanged. Activity loads older canonical
History pages through `next_before`, retaining the loaded page depth on refresh.
Board, discussion, audit and Usage replies commit only for the current selection
and request; delayed reads cannot overwrite newer navigation.
Evidence: `SwarmPage.test.js` and `test_swarm_store.py`.

Board reads retain each post's saved UTC timestamp. The page formats it in the
host-provided timezone and separates the author/time header from the body.
Board and participant lists share ID-derived avatar colors and name initials;
the full author name remains visible independently of color. The presentation is
stable across page remounts (`SwarmPage.test.js`) and does not change saved posts.
The wrapping participant roster and modal post action sit above the messages;
the complete User Prompt stays in the header, status beside the tabs, and the
Swarm id under Usage (SwarmPage.test.js).
Usage totals and participant Model rows abbreviate large counts with k/mio/mrd
and at most one locale-formatted decimal (SwarmPage.test.js).
The Usage page combines measured and estimated input/output counts as "Tokens used"
at total and Model-row scope. Tool Calls come from each participant's canonical
report and span its Model rows once, including participants without Model usage.
This is a Swarm presentation choice; canonical usage remains separated
(`SwarmPage.svelte`, `SwarmPage.test.js`).

Management Resume accepts an optional participant id. Store validation and
request replay bind that exact target; reopening a closed epoch resets only the
selected participant, leaving other participants unchanged. The existing group
admission path starts only the returned targets (test_swarm_store.py,
test_swarm_board.py). Activity offers this action for an inactive
participant and consumes canonical context usage from history and Run events;
it never substitutes cumulative Session usage.

The Store creates the complete current schema directly. It has no schema
upgrades or converter. Saved profiles and Swarm snapshots are consumed as stored;
input defaults are resolved when a profile is saved or previewed.

The page offers confirmed deletion after Stop. `swarms.delete` marks a closed
Swarm `deleting`, removes its bound participant Sessions through the host, then
transactionally removes its Board, participants, events and request receipts.
The profile and other Swarms remain. Start/Stop/Resume/Delete are serialized; the durable
deletion marker blocks Resume, including request replay, and survives restart so
a failed deletion can be retried. Tests: `test_swarm_board.py`, `SwarmPage.test.js`.
