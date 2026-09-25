# Swarm

Read for changes to the bundled Swarm Extension. The generic Extension contracts
remain in `extensions.md`; Swarm policy belongs under `resources/extensions/swarm/`.

Private Session Tools use owner-selected call repair before scoped execution (`_tool_calls.py`; details under the Board section), including encoded counts and known wrappers. Bound identities remain exact; optional values are not erased merely because their schema rejects them. Field aliases are explicit lists (the `limti` typo among them); there is no general nearest-name matching of field names. Inbox and State accept their single action under common synonyms (`receive`, `status`) and reject other actions with a message naming the Swarm Tool that has them. Recognizable wrappers and explicit identities matching the bound Session are accepted (the identity fields and redundant action labels are declared `unadvertised_parameters`, so dispatch validates them without offering them); unsupported operations and scope mismatches remain failures. The probe executes the Model's emitted arguments, compares them with independently prescribed conformance inputs, and verifies canonical receipts.

Swarm Activity uses live `run_active` as authoritative over an older persisted Session status. Failed/interrupted participants remain visible in an overview warning, and partial Resume failures name the affected participants after refresh. `swarms.get` inspects every participant's lifecycle Run with one batched `temporary_agents.owned_runs` read; only a Run id without an owned record counts as inactive, and unexpected host failures propagate instead of looking idle. Completion callbacks arriving after Stop or a newer lifecycle epoch quietly defer to that lifecycle outcome (`test_swarm_lifecycle.py`, `SwarmPage.test.activity-and-usage.test.js`). Temporary participants inherit the Runtime streaming loop; request diagnostics use the shared Chat timeline.

## Owners

- `extension.py` owns registration, management operations, the four Tool handlers,
  and coordination with owner-bound temporary execution groups. The Board handler
  delegates to `_board_tool.py` (call checks, actions, corrections) and
  `_board_view.py` (pure recipient resolution and result text); `_tool_calls.py`
  holds the per-Tool call-syntax normalizers.
- `store.py` owns the SQLite profile, Board, Wiki, audience, delivery, lifecycle and audit
  transactions. It receives canonical receipt lookups; it must
  not open the Session database directly. Its database handle comes from
  `host.open_database("swarm", SCHEMA_SQL)` at startup (kernel database
  `ext.swarm.swarm`, file `extension-data/swarm/swarm.db`; `extensions.md` ->
  Extension databases). Swarm never opens SQLite files itself, and the host
  closes the handle after shutdown.
- `agent_text.py` and `wiki_text.py` own the scoped Tool definitions and reviewed
  runtime wording. `_store_wiki.py` implements Wiki transactions within the existing
  Swarm database owner.
- `ui/SwarmPage.svelte` and `ui/ProfileEditor.svelte` own the domain page. The app
  shell and page bridge remain generic; built assets live in generated `web/`.
- Retained-list refreshes read profiles and Swarms. Model, Tool, Skill and
  Project choices load when opening a profile editor; the Run form also loads
  the catalog to resolve a selected Project default, not on display context
  updates. Failed editor loads leave the overview usable (`SwarmPage.test.js`).
- Profile editing uses the shared Model search/selection and effort helpers,
  the shared Secondary bar, topic tabs, a bounded scrollport, and the shared
  `SaveButton` after its fields (Save / Saving… / Saved, reflecting unsaved edits).
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

Profiles expose all four private Swarm Tools under Tools & Skills, using the existing `tool_access.denied` policy for individual switches. All/None also enables/disables the current private set. The public Tool catalog remains unchanged; the Swarm editor appends owner-private catalog entries. Choices apply to future executions, while Resume retains the saved snapshot. Disabling a Tool does not remove its human tab or stored content and does not change independently configured Board delivery/wakes. Preview and actual Model definitions respect the same denials (`test_runtime_extension_host.py`, `test_swarm_lifecycle.py`, `SwarmPage.test.profiles.test.js`).

## Invariants that affect changes

The human UI calls reusable profiles "Swarms" and their executions "Runs";
management operations and persistence retain their profile/swarm identifiers.
Profiles are versioned, revision-checked starting configurations. A started Swarm
retains its prompt and configuration snapshot. The formation is fixed; participant
Sessions persist across explicit Resume. Temporary execution ownership belongs to
`core/agents/temporary.py`, not to an Identity Agent or a parent Session.
Saving without a command shortcut generates a unique name-based shortcut inside
the profile transaction; omission on an update retains the existing shortcut.
Explicit shortcuts remain validated and collision-checked (`store.py`).

New Swarms atomically retain the original user request as an immutable user-authored
Board post at sequence zero. `goal_post_id` identifies it in the Swarm snapshot. The
page pins it separately; chronological discussion pages exclude it, while
exact-message reads return it. The Board Tool's list result, and main-discussion
reads that reach the start, carry a `user_request` line with the copyable read call. It creates no delivery audience: the initial
participant message points to this post and asks Agents to read and discuss the
request together before implementation. The Agents decide when they are ready to
act; no fixed roles, discussion rounds, plan template or approval phase are imposed.
Resume preserves admitted Session history and supplies the same initial message to
participants not yet admitted. Existing profiles and historical messages are not
rewritten. New profile defaults orient participants toward the shared collaboration Tools available to them. When Board is disabled, the initial message carries the exact original request directly. With Wiki available it retains peer discussion guidance; with Wiki disabled it avoids requiring inaccessible collaboration. Disabling Inbox also removes Inbox-specific delivery guidance.
Evidence: `agent_text.py`, `test_swarm_lifecycle.py`, `test_swarm_wiki.py`.

`swarm_wiki` is available only to bound participant Sessions of the owning Swarm.
The human page and CLI use the equivalent `wiki` management operation. Pages are
free Markdown with stable `page_id` values and `#wiki/<page_id>` links that navigate
inside the Swarm page. Agents choose how to organize them. List/search returns
recent changes and excerpts; content and history reads are bounded. List/history
continuations bind their query and revision watermark; content continuations pin
the requested revision. Search matches Unicode-casefolded titles and content; the
folding runs in Python over the latest revisions because kernel read connections
carry no custom SQL functions.

Create, update, delete and restore preserve full versions with author and timestamp.
Update supports title/content replacement or one unique `old_text`/`new_text` edit.
Writes require payload-bound request ids; changes to existing pages also require
`expected_revision`. Targeted edits reuse `core.tools.fuzzy_match.replace_fuzzy`
with only its precise strategies (`precise_only=True`): typography, newline,
whitespace and indentation differences are tolerated, but every `old_text` line
must match, so similarity never selects a different passage at any revision
(user decision A, `test_wiki_old_text_must_match_every_line_precisely`). With an
older revision, only a targeted edit without a title change may proceed.
Missing/ambiguous matches fail atomically. Full
replacement, title changes, delete, restore and future revisions retain strict
revision checks. Recovery reads the current page and reconciles the edit. Delete retains history,
and restore creates a new live revision from the chosen historical content.
Wiki edits invalidate the human page but create no Board messages or participant
wakes. Agents share page links on the Board when they want attention.
Evidence: `_store_wiki.py`, `test_swarm_wiki.py`, `swarm_wiki_cases.py`.

Board posts are immutable and public within one Swarm. `swarm_board` can ping
participants on a discussion's opening message without joining those recipients;
creation, opening-message audience and the main-discussion announcement commit
atomically. Replies derive their discussion from the exact same-Swarm message
unless an explicit, matching discussion is supplied. Reads start with newest
posts, chronological within each page. The Tool continues to older posts with
`before` (the oldest shown post id); Store read cursors remain accepted.
The Board UI reverses each page for newest-first display and appends older pages
below it; this presentation does not change the Store or Tool read order.
Ordinary post bodies use the shared `MarkdownContent.svelte` renderer and Chat
typography, including fenced-code Copy actions. Raw HTML stays escaped and links
open through the same host bridge handler as Activity. Discussion announcements
retain their dedicated navigation action (`SwarmPage.test.js`).
Coverage: `test_swarm_board.py` and the production `swarm_tool` probe.

Swarm Tool calls follow the Tool error-tolerance rules (`../tools.md`). Owners:
- `_tool_calls.py` repairs call syntax for all four Tools before validation:
  wrapper objects, other harnesses' field and action names, an action implied by
  the supplied fields, placeholder values in optional fields, and echoed
  idempotency keys the handler derives. An action that belongs to another Swarm
  Tool fails with a message naming that Tool.
- `_board_tool.py` checks Board calls. It drops fields that request nothing, such
  as paging fields on post, with a `note`. It clamps `limit` above 100 with a note.
  It rejects a field that another action would use, naming those actions. It
  corrects a read-only reference with a note when exactly one candidate exists:
  a post number (`pst_N`), a close post id, a close discussion id, or a post id
  passed as `cursor`. It never corrects a write target (`reply_to`, a post's
  `discussion_id`, recipients); those fail before any effect with the exact
  corrected call.
- `_board_view.py` renders results as plain text: one header line per post
  (`[post_id] Author (in ...; reply to ...; pinged ...)`), then its verbatim
  text. Copyable continuation calls follow as JSON.

The Store keeps its exact-id contracts; `post_suggestions` only feeds these
corrections and errors.

The Board Tool description guides participants toward
the main discussion for shared conversation and coordination; additional discussions
are for several Agents working through a specific problem. New announcements carry
readable text, exact discussion/opening-post IDs and read/join guidance. Human Board
reads additionally resolve the discussion target from its creation request outcome,
so the page can render a localized navigation action without parsing message text.
Ordinary posts cannot acquire that action by copying an announcement's content.
Saved posts and profile snapshots are not rewritten. Evidence: `agent_text.py`,
`test_swarm_store_board.py`, and `SwarmPage.test.js`.

Audience snapshots survive later join/leave changes. A public ping takes precedence over discussion/main
routing for that recipient, without creating duplicate deliveries. Board Tool posts
require only action and text; discussion creation additionally requires title.
Board callers do not supply request ids. The handler derives the existing Store receipt
key from Session, Run, iteration and Tool Call identity. Separate Tool Calls with
identical content create separate posts. Management mutations retain their
payload-bound request ids.

Preparing a batch is not delivery. Only a matching canonical Session receipt
acknowledges its contents. Tool batches are acknowledged after their complete
carrier is saved. Successful automatic and Tool delivery acknowledgments publish
a page invalidation, so pending counts refresh during an active Run without
waiting for another Board mutation or Run completion. Failed acknowledgments
retain pending state; empty Tool batches do not invalidate the page. Evidence:
`test_swarm_inbox.py`, `test_swarm_lifecycle.py`, `SwarmPage.test.js`.
Delivery mode and idle wake permission are independent. A wake
always delivers actual pending Board content, including pull-mode messages on a
wake-enabled route; it never asks the Agent to fetch the first batch. Bounded
overflow remains available through Inbox. Read pages are bounded and cursors
cannot cross queries.

Background wake scans prepare batches only for idle participants, checking state
inside the Store transaction. Running participants select pending content at the
next Model request, so a concurrent Inbox or Board read cannot leave a prematurely
prepared wake batch that later repeats its delivered messages (`test_swarm_inbox.py`).
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
Saved names survive request replay, restart and Resume; stored recipients remain
participant ids (`test_swarm_store.py`). At its boundary the Board Tool also resolves
exact display names, `all`/`*` (every other participant), and words meaning the
user. The user is not a participant, so that ping is dropped with a note. Values
matching no participant fail with the roster and any unique close id; the Tool
never guesses between participants (`test_swarm_board.py`). Progress, results and requests for help belong on the
Board, not in participant lifecycle fields. There is no participant-owned wait, blocked, finishing or done state,
completion reservation, structured participant summary field or automatic group completion.

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
Evidence: `test_swarm_store_execution.py`, `test_swarm_board.py`, `test_swarm_lifecycle.py`.

Run callbacks recompute the open Swarm's aggregate state: any running peer keeps
it running, unsuccessful inactive peers require attention, and all-idle peers
yield idle. Repeated start/wake acknowledgments cannot overwrite the same Run's
terminal outcome. The page's active Run indicator uses exact canonical Run
inspection, not the presence of a retained Run id.

The Store's integer lifecycle epoch and the temporary facade's opaque admission
epoch are different identities and are linked explicitly. Stop closes execution
admission; the Board still accepts user posts. A new human Board post after Stop or
startup interruption persists first, then resumes the same participant Sessions.
Posting serializes with Stop/Resume/delete so a message sent during draining waits
for it to finish. Resume prepares delivery without scheduling an additional
automatic wake. Replaying a saved post never restarts a later stopped execution;
participant Tools retain their epoch checks. Records are not deleted. Startup
recovery marks unfinished execution interrupted and never admits work automatically. Stale callbacks cannot reuse an
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
`test_swarm_store_execution.py`, `test_swarm_lifecycle.py`.

## Verification routes

Store source routing: `store.py` retains asynchronous validation/admission and the public `SwarmStore` API. `_store_database.py` wraps the host-opened kernel `Database`: each `_write` is one kernel write transaction (retried as a whole on a busy database), each `_read` one read transaction, and the signed cursor key is cached at open. `_store_profiles.py`, `_store_lifecycle.py`, `_store_delivery.py`, `_store_reads.py` and `_store_board.py` implement concrete operations against that database capability; they do not receive the Swarm service or public Store. Shared row checks/projections live in `_store_records.py`, pure input/value rules in `_store_values.py`, and the existing name pool in `_participant_names.py`. Worker dispatch (`SwarmDatabase.run`) runs each operation on the host database's own worker pool through `Database.run_async`, without a Store lock or a Swarm pool; once the host closes the database, Store operations raise the kernel's `DatabaseUnavailableError`; the kernel serializes writes, so a state-dependent decision belongs inside the write operation, not in a preceding read. Delivery receipt lookups run outside any database transaction. Store tests open the same kernel spec offline through `open_swarm_database` (`tests/resources/extensions/swarm_store_helpers.py`).

Internal Extension source routing: `extension.py` owns the live Swarm service and participant callbacks; `_extension_values.py` holds pure argument/projection/configuration helpers, `_operation_schemas.py` the management schemas, and `_registration.py` binds the existing service to Extension capabilities. Registration constructs that service lazily to keep imports acyclic. Tool/schema/reminder wording is preserved.

- Board/profile/policy transactions, races, receipt recovery and lifecycle:
  `tests/resources/extensions/test_swarm_store.py` (profiles),
  `test_swarm_store_board.py`, `test_swarm_store_delivery.py`, and
  `test_swarm_store_execution.py`.
- Registered private Tool behavior and management boundaries:
  `tests/resources/extensions/test_swarm_board.py`.
- Actual Chat participants, terminal proofs and Stop/Resume:
  `tests/resources/extensions/test_swarm_lifecycle.py`.
- Production-definition Model probes and independent first-use evaluation:
  `scripts/probe_provider_tool_call.py` (`swarm_tool` scenario), with probe tests
  under `tests/scripts/test_probe_provider_tool_call_extensions.py`. The `unassisted` case
  supplies the production initial message pointing to the original goal post, with
  all four private Tools available. Success requires reading that goal, receiving peer
  feedback, a later public contribution, and a normal final response. It evaluates
  coordination effects, not the semantic quality of the generated checklist.
  The `swarm_wiki` matrix exercises every action, repair and conflict handling, and
  checks durable effects independently of the Model's emitted arguments.
  The probe purges cached Extension modules before loading its own checkout.
- Rendered business controls: `webui/src/components/__tests__/SwarmPage.test.js`
  and `SwarmWiki.test.js`;
  generic iframe isolation is tested by ExtensionPage and server asset tests.

Use the Sessions, Runs, Chat and Statistics maps before changing their owning
contracts. Swarm must consume their canonical history and usage instead of
maintaining a second transcript or authoritative usage counter.

The retained Swarm list projects a bounded first-line goal title from the stored
prompt; `swarms.list` replaces it with the canonical group title when one exists.
After a successful Start (not a replay), a background task calls
`temporary_agents.title_group` with the goal, then publishes a `swarms` change so
the page refreshes; failures are logged and the Run continues, and `close()` cancels
pending title tasks. Runs started before titles existed keep the goal-line title.
The sidebar groups preparing/running/stopping records as Active runs;
all other states, including idle and needs_attention, appear under Inactive runs.
This presentation does not close an idle execution group or disable its Board wakes.
Swarm selection opens the editor; the pinned New run row returns to the goal form
and is marked current while that form shows.
The form prefills the directory after Swarm selection and preserves manual edits
during invalidation. Project defaults resolve through the catalog; unchanged
defaults retain the explicit Project selection. A changed directory is sent as
the optional absolute-path `swarms.start.working_directory` argument and selects
directory-only execution, without inferring Project identity from a path.
Start validates the effective directory and catalog before creating Sessions.
The stored profile and profile snapshot stay unchanged; `effective_configuration`
records the Run directory and Project selection for Resume and request replay.
Evidence: `SwarmPage.test.js` and `test_swarm_lifecycle.py`.
Activity forwards running Tool Call cancellation through the generic page bridge with the exact Swarm group, Run and Tool Call ids. The host verifies current page registration and canonical Run ownership before requesting call-local cancellation; failures stay visible and the Run continues (`SwarmPage.test.activity-and-usage.test.js`, `test_extensions_methods.py`).
Participant selection opens Activity and disposes the previous Run subscription;
late history/subscription replies cannot replace a newer participant selection.
Terminal Run events reload canonical history so non-streamed final output appears
without reopening Activity. Refresh and reconnect also reload the open History
and reattach an active Run when its id is unchanged. Activity loads older canonical
History pages through `next_before`, retaining the loaded page depth on refresh.
Board, discussion, audit and Usage replies commit only for the current selection
and request; delayed reads cannot overwrite newer navigation.
Evidence: `SwarmPage.test.js` and `test_swarm_store_board.py`.

Board reads retain each post's saved UTC timestamp. The page formats it in the
host-provided timezone and separates the author/time header from the body.
Board and participant lists share ID-derived avatar colors and name initials;
the full author name remains visible independently of color. The presentation is
stable across page remounts (`SwarmPage.test.js`) and does not change saved posts.
The header shows the snapshot Swarm name and state. The Board holds the single
collapsible user request and working directory. Compact participant buttons show
names, identity colors and execution dots; Model/status/pending details are in
hover/focus tooltips. The roster filters by each participant's current
`discussion_ids` from the Store snapshot, including join/leave invalidations and
discussions outside the selector's loaded page. Pings alone do not join a peer.
Post backgrounds and left borders share the stable author color. The Swarm id
stays under Usage (`SwarmPage.test.js`, `test_swarm_store_board.py`).
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
admission path starts only the returned targets (test_swarm_store_execution.py,
test_swarm_board.py). Activity offers this action for an inactive
participant and consumes canonical context usage from history and Run events;
it never substitutes cumulative Session usage.

`SCHEMA_SQL` in `_store_database.py` is the declared schema; the kernel creates it
and reconciles later additive changes on open. Keep changes additive (new tables,
indexes, nullable or defaulted columns); state and kind values are validated in
code, never by CHECK enums. New timestamps use the canonical UTC form
`YYYY-MM-DDTHH:MM:SS.ffffffZ` (`_store_values._now`). A database from before
Persistence Generation 1 is converted by
`scripts/converters/persistence_generation_1/swarm.py` (every table with its keys
and the cursor key, canonical timestamps; refused rows are dropped and reported;
round trip through the current Store in `tests/scripts/converters/persistence_generation_1/test_swarm.py`).
The converter also replaces retired Tool names in the `tool_access` of saved profiles
and Swarm snapshots (`database/generation-1-conversion.md` -> Retired Tool names) and
drops the retired `inactive_recipients` from stored Board request outcomes (same file ->
Retired fields and values).
The Store has no upgrade code. Saved profiles and Swarm snapshots are consumed as stored;
input defaults are resolved when a profile is saved or previewed.

The page offers confirmed deletion after Stop. `swarms.delete` marks a closed
Swarm `deleting`, removes its bound participant Sessions through the host, then
transactionally removes its Board, Wiki pages and revisions, participants, events
and request receipts.
The profile and other Swarms remain. Start/Stop/Resume/Delete are serialized; the durable
deletion marker blocks Resume, including request replay, and survives restart so
a failed deletion can be retried. Tests: `test_swarm_board.py`, `SwarmPage.test.js`.

Management operation descriptions state each action and its continuation or revision requirements. The CLI lists compact descriptions first and exposes the complete argument schema through per-operation help. Profile save/preview help includes a validator-checked creation example, optional fields, and revision guidance. Source: `_registration.py` registration and `cli/extensions_management.py`; tests: `tests/resources/extensions/test_mcp.py` and `tests/cli/test_extensions_operations.py`.

Private UI routing: `ui/SwarmPage.svelte` composes the page; `pageModel.svelte.js` retains its management state, request generations, Board/Usage loading, mutations, and bridge lifetime, while `pageActivity.svelte.js` owns participant History/replay subscriptions and context projection. `pagePresentation.js` holds display-only count/avatar helpers. `ProfileEditor.svelte` retains profile drafts, validation, and autosave; `profilePromptPreview.svelte.js` owns preview request ordering and freshness. Adjacent `swarmPage.css` and `profileEditor.css` scope styles to their page/editor surfaces, including portaled dialogs.

Background invalidations are coalesced by the Swarm-internal `ui/pageRefresh.js`:
each mounted page/panel has one refresh in flight and at most one pending pass,
with a fixed scheduling window that continuous traffic cannot postpone. The
overview reloads Board, Usage, or audit data only for the visible tab; selecting
a tab loads its current data independently of hidden reports. Activity retains
the existing History and live subscription while its participant's Run identity
and active state are unchanged. A new Run or terminal state reconciles History through the generation/sequence append cursor, preserving older loaded pages;
subscription failures remain retryable on a later invalidation. Coverage:
`SwarmPage.test.performance.test.js` and `SwarmPage.test.reconciliation.test.js`.

WikiPanel.svelte owns free page drafts, bounded content loading, search, version history and restore. Its state remains mounted for the selected Swarm across tab changes, while hidden tabs render no controls and schedule no refreshes. Reopening shows retained entries/content immediately while checking current revisions; unchanged open pages are not re-read on unrelated invalidations. Late background reads cannot replace a newly opened page or an edit draft. Existing pages autosave and flush before local or shell navigation; new pages save explicitly. Conflicts retain the draft and block navigation until it is saved or explicitly discarded. Invalidation refreshes discovery without replacing an open edit. The Extension-page bridge remains generic; the Wiki adds one management operation. Regression coverage includes SwarmWiki.test.js and `webui/src/components/__tests__/SwarmPage.test.js` plus the bundled-page build test.


The Decisions Tool, management operation, tab, and linked-question enrichment are
removed. Board discussion and Wiki pages cover shared deliberation and retained
results. The generation 1 schema has no decision tables; the converter drops
their retained rows and the `decisions:` request receipts.
No saved profile instructions, Swarm snapshots, or Session history are rewritten.

Wiki search uses explicit button callbacks and an input Enter handler because the
isolated page sandbox blocks native form submission. The page does not require
`allow-forms`.

Swarm Activity retains one shared Chat event projection, compresses deltas and
flushes live updates in 33 ms batches. Initial replay is buffered through the
server-reported `replay_through_sequence` before replacing displayed History;
opening a long-running Session therefore does not visibly replay its old steps.
The shared Chat Timeline uses explicit complete Run descriptors to retire retained live output; stale History or active
snapshots cannot erase newer output or restart a settled subscription. In-flight
inspection survives peer invalidations, and Thinking disclosure state belongs to
the inspected Session. Tests: `SwarmPage.test.streaming.test.js` and the existing
Activity/reconciliation suites. The generic bridge only relays the replay
watermark (`ExtensionPage.test.js`, `test_extensions_methods.py`).
Activity and Board reuse the same compact participant chips; Activity retains
all Swarm participants and marks the selected Session with a pressed state.

The human page omits the Delivery audit tab; internal events and canonical receipts
remain available for diagnosis. Usage requests one combined group report, including
participant breakdowns, and shares an in-flight request across refreshes. It retains
the last report while refreshing silently, without transient progress text. Evidence: `test_swarm_lifecycle.py`,
`SwarmPage.test.activity-and-usage.test.js`, `SwarmPage.test.reconciliation.test.js`.

Pending Board reads, automatic delivery, and participant counts use the partial
`recipients_pending_participant` index, so delivered history does not dominate
Swarm's serialized database operations. The index is created with the current
schema on open, without changing retained records. Ordered pending reads explicitly
select this index so SQLite does not scan delivered posts to satisfy sequence order.
Discussion Post pages likewise select `posts_discussion_page`, so a page of one
Discussion does not walk the whole Swarm's Posts by sequence. Every index in
`SCHEMA_SQL` names its reader in the comment above it.
The regression fixture checks
bounded SQLite work with 12 peers and 22,000 delivered recipient rows
(`test_swarm_store_delivery.py`). A separate Wiki test verifies concurrent edits
to 12 independent passages from the same observed revision without lost changes
(`test_swarm_wiki.py`); this does not measure Model collaboration quality.
