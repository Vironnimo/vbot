# Swarm

Read for changes to the bundled Swarm Extension. The generic Extension contracts
remain in `extensions.md`; Swarm policy belongs under `resources/extensions/swarm/`.

Private Session Tools use owner-selected call repair before scoped execution (`_tool_calls.py`; details under the Board section), including encoded counts and known wrappers. Bound identities remain exact; optional values are not erased merely because their schema rejects them. Field aliases are explicit lists (the `limti` typo among them); there is no general nearest-name matching of field names. Inbox and State accept their single action under common synonyms (`receive`, `status`) and reject other actions with a message naming the Swarm Tool that has them. Recognizable wrappers and explicit identities matching the bound Session are accepted (the identity fields and redundant action labels are declared `unadvertised_parameters`, so dispatch validates them without offering them); unsupported operations and scope mismatches remain failures. The probe executes the Model's emitted arguments, compares them with independently prescribed conformance inputs, and verifies canonical receipts.

Swarm Activity uses live `run_active` as authoritative over an older persisted Session status. Failed/interrupted participants remain visible in an overview warning, and partial Resume failures name the affected participants after refresh. `swarms.get` inspects every participant's lifecycle Run with one batched `temporary_agents.owned_runs` read; only a Run id without an owned record counts as inactive, and unexpected host failures propagate instead of looking idle. Completion callbacks arriving after Stop or a newer lifecycle epoch quietly defer to that lifecycle outcome (`test_swarm_stop_resume.py`, `SwarmPage.test.js`, `SwarmPage.activity.test.js`). Temporary participants inherit the Runtime streaming loop; request diagnostics use the shared Chat timeline.

## Terms

### Addressed participant
A participant a Board post names after `@` in its text (display name or participant id, any case), whose post it answers (`reply_to`, an unadvertised caller option), or whom a caller lists in explicit `recipients`; never the author. A name without `@` is only a mention. The Store saves them in `recipients_json` and gives them the `ping` route. "Ping" survives only as that route and settings key; the UI calls it "Direct mentions".

### Post number, discussion number, page number
The short references participants see and send: `#N` is a post's Swarm-wide `sequence` (the goal post is `#0`), `dN` a discussion's `sequence` (the main discussion is `d1`), and `wN` a Wiki page's position in creation order (the first page is `w1`). The Store resolves them to the stored `pst_`/`dsc_`/`wpg_` ids inside each operation, so saved rows and the schema keep those ids, and exact stored ids stay accepted.

### Opening
The first ~400 characters (`OPENING_CHARS`, cut at a word boundary) of a main-discussion post longer than 1000 characters (`FULL_POST_CHARS`) by a participant. Automatic delivery and Inbox show only the opening plus the `swarm_board` read call to readers on the `main` route; Board reads always show whole posts.

### Quiet period
The in-memory wait after a participant's completed Run in which it used no Tool (`_wake_pacing.py`). During it, only posts addressing it and posts by the user wake it.

## Owners

- `extension.py` owns registration, management operations, the four Tool handlers,
  and coordination with owner-bound temporary execution groups. The Board handler
  delegates to `_board_tool.py` (call checks, actions, corrections) and
  `_board_view.py` (pure recipient resolution and result text); `_tool_calls.py`
  holds the per-Tool call-syntax normalizers. `_wake_pacing.py` holds the
  in-memory quiet periods that narrow which posts wake a participant.
  Management operations log once in `extension.py` after the change (`logging.md`):
  profile created/updated (changed field names)/deleted, delivery settings updated
  (changed routes), Swarm started/stopped/deleted/resumed (`reason=request|post`;
  WARNING when a participant's Run could not be admitted). Replays and no-ops are
  silent; the lines carry ids and counts, never goals, posts or profile text. Page
  operations and `/swarm` share the operation surface, so the lines name no actor.
  Startup logs one line when it acknowledged saved delivery batches or found
  conflicting receipts (counts only; WARNING for conflicts).
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
- `ui/SwarmPage.svelte` and `ui/ProfileEditor.svelte` own the domain page.
  `SwarmPage.svelte` is the page root; its areas (the shell with sidebar, Run
  start form, Run view and dialogs; the Board; Activity; Usage) have their own
  components, model modules and stylesheets (Private UI routing below). The app
  shell and page bridge remain generic; built assets live in generated `web/`.
  `ui/i18n.js` holds the page's `swarm.*` English text; `ui/main.js` registers it
  with the WebUI catalog before mounting, and `SwarmPage.support.js` does the same
  for the page tests. The WebUI i18n catalog guard checks these keys like core
  keys, within this page only.
- Retained-list refreshes read profiles and Swarms. Model, Tool, Skill and
  Project choices load when opening a profile editor; the Run form also loads
  the catalog to resolve a selected Project default, not on display context
  updates. Failed editor loads leave the overview usable (`SwarmPage.test.js`).
- The profile editor is one continuous configuration page in the shared
  section grammar (`webui/design.md` -> Settings, Agents, Projects, and
  Schedules): Swarm, Participants, Working directory, Instructions, Tool access,
  Skills, Communication, Compaction and a closed Advanced section (Swarm ID,
  deletion), ending with the shared save state (`webui/autosave.md`). It uses
  the shared Model search/selection and effort helpers.
  Creation saves explicitly (Create Swarm or Cancel). Like a new Cron job
  (`webui/source-map.md`), a new Swarm's changed draft outlives its editor in the page
  model (`newProfileDraft`): leaving it by any page navigation, Back/Forward
  included, keeps it without asking, and New Swarm continues it; Cancel asks
  before discarding it, and a successful Create ends it. Saved profiles
  autosave and flush before navigation through the generic page bridge; when
  leaving cannot save, a dialog offers Retry (Keep editing for incomplete
  settings) or discarding. Field errors appear under the field's label; only a
  save the user asked for moves focus to the first invalid field, while a
  background autosave shows the errors in place. The batching limits are
  checked here with the server's ranges (`DELIVERY_LIMITS`), so an empty or
  out-of-range limit is such a field error: never saved, never dropped.
  Invalidations preserve the mounted draft. Leaving the Swarm page itself
  unmounts the page, so a new Swarm's draft ends there.
  The Instructions section owns editable instructions, explicit block
  selection, event reminder switches and Model-specific combined previews with
  the separately sent Tool definitions (shared `ToolDefinitionsPanel`). A
  preview needs what a save needs except the name (`profiles.preview` names an
  unnamed draft "Preview") and a free shortcut, so a missing or invalid setting
  shows its field error and takes focus instead of sending a request; until a
  save shows every problem, the errors a preview shows leave out those only a
  save has. New
  profiles receive the complete default from `agent_text.py` through the editor
  catalog; Swarm no longer registers a separately appended orientation block.
  The visible value is saved as-is, including an intentionally empty value;
  existing profiles are not backfilled and runtime adds no fallback.
  Omitted effort uses the Provider default, not shared Agent defaults.
  All/None Tool actions materialize a selected policy through `toolAccess.js`.
  Skills use the shared Skill selection panel: adding new Skills automatically
  is the `['*']` wildcard, and changing a single Skill turns it into the
  explicit list of allowed Skills (saved names the catalog does not list are
  kept). The Team agent row shows while the Swarm works in a Project with Team
  agents; changing the working directory drops Team agents the new Project does
  not offer, because saves reject them.
  `DeliverySettings.svelte` edits one `delivery` value for any caller (per
  route: when messages are received and whether a message starts an idle
  participant; the batching limits behind a disclosure row) and holds no save
  logic; numbers are reported as entered and normalized by the caller
  (`normalizeDelivery`). The caller passes the messages of invalid limits by
  field (`errors`); each shows under its field, which is marked invalid and
  described by it, and the disclosure opens for an error and does not close
  while one remains. Limit ids and ranges come from `DELIVERY_LIMITS`.
  Tests: `SwarmPage.profiles.test.js` and `test_swarm_store_profiles.py`.

Profiles select every additional System Prompt block explicitly; Tool Call Style,
System reminders and Skills start enabled, other blocks (including Runtime and Working Project)
start disabled, and newly registered blocks stay disabled. This selection controls
prompt text independently of working directory and Tool/Skill access. It is
persisted in temporary Agent bindings and each started Swarm's profile snapshot.
`profiles.preview` accepts an unsaved profile and formation row, using the host's
read-only prompt inspection with the same Project context and Model Tool routing.
It returns rendered block details and separately transmitted Tool definitions;
draft changes invalidate the displayed preview. Evidence:
`test_runtime_extension_host.py`, `test_prompts_layouts_overrides.py`, `SwarmPage.profiles.test.js`.

Delivery and explicit Resume guidance are individually
switchable in the snapshot. Their complete wording is inspectable in the editor.
Disabling guidance preserves Board payloads and lifecycle behavior; empty
continuation receipts add no Model-visible reminder. Existing Session history is
not rewritten. Evidence: `extension.py`, `test_swarm_store_profiles.py`,
`test_swarm_wakes.py`, `test_swarm_stop_resume.py`.

A profile's Tool and Skill selection applies as saved, also when the Swarm works in a Project whose Tool or Skill whitelist is narrower; the Project supplies directory, Skills and context (`projects.md`). Profiles expose all four private Swarm Tools under Tool access, using the existing `tool_access.denied` policy for individual switches. All/None also enables/disables the current private set. The public Tool catalog remains unchanged; the Swarm editor appends owner-private catalog entries. Choices apply to future executions, while Resume retains the saved snapshot. Disabling a Tool does not remove its human tab or stored content and does not change independently configured Board delivery/wakes. Preview and actual Model definitions respect the same denials (`test_runtime_extension_host.py`, `test_swarm_start.py`, `SwarmPage.profiles.test.js`).
`swarm_inbox` is offered only when some route (`main`, `discussion`, `ping`) can hold messages back, i.e. its mode is not `all`: with every route on `all` (the default), pending posts reach the next Model request anyway, and Inbox-only requests cost 3-10% of input in the eight analyzed Runs (Sessions, 2026-09). `_participant_config` decides from the profile (snapshot) delivery and denies it through `_tool_access` when a Session is created (start, Resume of a missing participant, preview). Live `swarms.settings` changes do not add or remove it from existing Sessions; Agent texts check `_inbox_available(binding)` (`test_swarm_inbox_is_offered_only_when_a_route_can_wait`).

Profiles carry an optional `compaction_policy`. `null` (materialized on save) or an
absent key (profiles and snapshots saved before the field; no backfill) keeps
participants on the normal chain, i.e. global Compaction settings; an object is
normalized as one complete Policy by the shared Settings normalizer
(`_store_values._compaction_policy`). Save replaces the whole profile, so omitting
the field on an update returns to inheritance; only `slug` is retained on omission.
`_participant_config` passes the snapshot value into every participant's
`TemporaryAgentConfig`, where it is the Agent-level Policy for automatic and manual
Compaction (`agent.md`, `compaction.md`). Edits apply to future Runs; Resume creates
missing participants from the saved snapshot. The editor's Compaction section
reuses the shared `CompactionPolicyEditor` rows; turning the override on starts
from the WebUI default Policy because the catalog carries no global Policy
(`test_swarm_store_profiles.py`, `test_swarm_start.py`, `SwarmPage.profiles.test.js`).

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
Board post at sequence zero. `goal_post_id` identifies it in the Swarm snapshot, and
`goal_post_sequence` gives its post number. The
page pins it separately; chronological discussion pages exclude it, while
exact-message reads return it. The Board Tool's list result, and main-discussion
reads that reach the start, carry a `user_request` line with the copyable read call. It creates no delivery audience: the initial
participant message names the exact `swarm_board` read call for this post (27% of
observed first Board calls listed discussions first) and asks Agents to read and discuss the
request together before implementation. The Agents decide when they are ready to
act; no fixed roles, discussion rounds, plan template or approval phase are imposed.
Resume preserves admitted Session history and supplies the same initial message to
participants not yet admitted. Existing profiles and historical messages are not
rewritten. New profile defaults orient participants toward the shared collaboration Tools available to them and ask for posts that add information, answer questions, report results or request action; acknowledgments of acknowledgments and empty closing posts are unnecessary. Existing profile instructions and started snapshots remain as saved. When Board is disabled, the initial message carries the exact original request directly. With Wiki available it retains peer discussion guidance; with Wiki disabled it avoids requiring inaccessible collaboration. Disabling Inbox also removes Inbox-specific delivery guidance.
Evidence: `agent_text.py`, `test_swarm_start.py`, `test_swarm_wiki.py`.

`swarm_wiki` is available only to bound participant Sessions of the owning Swarm.
The human page and CLI use the equivalent `wiki` management operation. Pages are
free Markdown with stable page numbers and `#wiki/w3` links that navigate
inside the Swarm page (links saved with a stored `wpg_` id keep working).
`_store_wiki._page_numbers` derives the numbers from each page's first revision,
so existing Swarms have them without a schema change. A page is never removed on
its own (delete saves a revision) and a new page's first revision gets the highest
revision id, so a number never changes or repeats (`test_swarm_wiki.py`).
Store results carry `number` beside the stored `page_id`; a continuation repeats
the call's own page reference. Store-assigned numbers were chosen over page names
that Agents pick: 3 of 11 pages were renamed in one Run, so a descriptive ID would
go stale or break links in immutable posts, and the title already carries the
meaning (Sessions, 2026-09). Agents choose how to organize them. List/search returns
recent changes and excerpts; content and history reads are bounded. List/history
continuations bind their query and revision watermark; content continuations pin
the requested revision. Search matches Unicode-casefolded titles and content; the
folding runs in Python over the latest revisions because kernel read connections
carry no custom SQL functions. An Agent `read` with `query` (Sessions, 2026-09)
reads the page as asked and adds `found`, before `content`: up to 5 lines of the
whole read revision containing the casefolded query, each with its line number and
starting character (the `offset` unit), or the `list` call that searches all pages
(`_wiki_tool.py::WikiCall._find`).

Create, update, delete and restore preserve full versions with author and timestamp.
Update supports title/content replacement or one unique `old_text`/`new_text` edit.
Store writes require payload-bound request ids. `swarm_wiki` callers do not supply
them: like the Board, the handler derives the key from Session, Run, iteration and
Tool Call identity, and drops an Agent-sent `request_id`; management keeps its own.
`expected_revision` is required only for whole-content replacement, so a newer
peer revision is never silently discarded; when present on other changes it is
checked strictly. A participant's whole-content update without it replaces the
current revision when that participant saved it, since no peer change can be lost,
and the Tool notes the replaced revision. The Store decides this inside the write
(`_store_wiki.py::_mutate`), so the payload stays stable and a replayed Tool Call
returns its saved outcome; management changes always need `expected_revision`. All 5
such refusals of one Swarm Run hit the caller's own revision (Sessions, 2026-09).
A change the page already holds (same title, content and deletion state, an
identical live page on create, an `old_text` edit already applied) saves no
revision and reports `unchanged`, before any stale check.
Targeted edits run `_wiki_edit.py`, aligned with `apply_patch`: precise
`replace_fuzzy` strategies (typography, newline, whitespace, indentation), then the
same edit without shared blank boundary lines, then already-applied detection.
`_wiki_emphasis.py` additionally recognizes a unique complete line of at least 12
words whose only copy differences are paired Markdown emphasis delimiters. Code,
links, escapes, block syntax, short fragments and changed targets are not emphasis
repairs; ambiguous candidates and literal marker differences are terminal. Inline
code spans follow CommonMark's backtick-string rule (an unclosed backtick string
stays literal) in one linear scan (`_code_spans`): the former backtracking regex took
18 s on a line of 2,000 backticks. The
page's emphasis survives in unchanged portions of the requested edit, and the
result names the original line with a bounded excerpt. Possible fenced code in
Markdown containers remains protected; a closing fence needs a whitespace-only
suffix. After that, unless the new text is already on the page, `copy_match.replace_copied`
for an `old_text` copied with errors (rules: `tools/apply_patch.md`). The page
keeps its own wording outside the change, so a copy error never overwrites a
peer's text; each differing line and respelled word returns in the result's
`notes` (`test_wiki_old_text_copied_with_a_misspelling_is_applied_and_named`,
`test_wiki_old_text_must_not_rest_on_other_text`). Ambiguity is terminal; a copy
resembling several passages sets `details.similar` and uses
`WIKI_AMBIGUOUS_SIMILAR`. Misses carry bounded line hints (closest passages, or the line holding
most of a one-line fragment) in `SwarmStoreError.details`, plus the first line where `old_text`
differs from the closest passage (`fuzzy_match.first_difference`, the diagnosis `apply_patch`
uses). The error shows each page line once, dropping a closest passage that overlaps a better
one, and names that first difference: overlapping windows showed two nearly equal passages
while the differing line lay beyond both excerpts (Sessions, 2026-09). With an older revision,
only a targeted edit without a title change may proceed. Title changes, delete,
restore and future revisions otherwise keep strict revision checks. Delete retains
history, and restore creates a new live revision from the chosen historical content.
Wiki edits publish a `wiki` change to the human page but create no Board messages or participant
wakes. Agents share page links on the Board when they want attention.
Evidence: `_store_wiki.py`, `_wiki_edit.py`, `test_swarm_wiki.py`,
`test_swarm_wiki_evidence.py`, `swarm_wiki_cases.py`.

`_wiki_tool.py` (`WikiCall`) owns the Agent side; the Store and the management
operation keep returning raw data (`next_call`, `current_revision`, excerpts) for
`WikiPanel.svelte`. Agents read plain text: read shows the revision as a sentence,
a `shown` range and a `more` continuation before the verbatim content; list and
history render one line per entry; mutations report a `status` sentence (delete
names the copyable restore call). Content mutations also return a bounded excerpt
from the exact saved revision, centered on the first actual changed position when
a line is long. The Store retains the excerpt in the mutation receipt, so peer
changes cannot alter a replay's evidence; continuations pin that revision.
Repairs that cannot change the effect run with a
note: `read` without `page_id` lists pages, a pasted link or quoted ID yields its
page reference, read-only calls resolve a page title or a unique close stored id
(`wpg_...`), create
ignores an unknown `page_id` and takes a missing title from the first Markdown
heading, `limit` above the action maximum is lowered, `expected_revision` is
dropped on read-only actions. Writes never resolve a guessed page; they fail with
the suggested page number, and an explicit `page_id` is never overridden. A passage
`update` (`old_text` with `new_text`) without `page_id` runs on the one live page whose
current content holds its exact `old_text` (`SwarmStore.wiki_pages(containing=...)`),
with the same checks inside that page as with its `page_id`, and notes the page it
used; the text is the Agent's own evidence for its page. Such calls failed 18 times in
two days, and in one Swarm Run all 9 retries repeated the update with the one page the
error named (Sessions, 2026-09). With several such pages the call fails naming them
(newest change first, at most 3); with none, or a whole-content `update` without
`page_id`, it fails with the list call. Page numbers (`w3` or `3`) and exact stored ids are
exact references in every action; the Tool names that page `w3` in the call before
running it, so results, continuations and errors show the number. Failures name
the next call: conflicts show the current revision
(content conflicts add a bounded diff since the base revision), deleted pages the
restore call, and a content update without `expected_revision` on a peer's revision
the current one.
Failed changes say "Nothing changed." in their first line.

Board posts are immutable and public within one Swarm. A post, including a
discussion's opening message, addresses the participants it names after `@` or
answers (Terms -> Addressed participant) without joining them to the discussion;
creation, opening-message audience and the main-discussion announcement commit
atomically. Name matching (`_store_values.mentioned_participants`) needs `@`
directly before the name, not after a word character (`x@Name` is no address).
Plain names addressed before: in the Run after that change 96% of posts
addressed someone, 4.3 participants on average, and about 70% of those names
were credits, possessives or ownership notes rather than direct address; 44% of
full deliveries of long posts and 28 of 42 cut-short Quiet periods came only
from such names. No post used `@` then, while 39% did in the Run before, when
`@Name` was the convention (Sessions, 2026-09). The `recipients` and
`reply_to` fields are unadvertised: they stay accepted and validated for callers
that send them, and the human compose form uses them, but Agents address by
writing `@Name`. `reply_to` added little for Agents: in one Run 79 of 585 Agent
posts used it and 57 of those also wrote `@` before the author's name, 577 posts
were in the main discussion, and its 16-character ids drew typos (Sessions,
2026-09). A `post` that names a post in `message_id` answers it, as with `reply_to`:
the intent is unambiguous, and the former rejection only cost a round trip; a
`message_id` that differs from `reply_to` still fails. Such a `post` without text
may have meant `read`, so its error offers the exact read call beside repeating
with text (an Agent that sent it gave up after the plain error, Sessions 2026-09).
Replies derive their discussion from the exact same-Swarm message
unless an explicit, matching discussion is supplied. Reads start with newest
posts, chronological within each page. The Tool continues to older posts with
`before` (the oldest shown post number); Store read cursors remain accepted.
The Board UI reverses each page for newest-first display and appends older pages
below it; this presentation does not change the Store or Tool read order.
The management `board.read` (human projection, not the Tool) has its own page
budget, independent of the delivery settings that bound Agent reads: whole posts,
at most `limit` and `HUMAN_PAGE_BYTES` (256 KiB) of text (`_store_reads.py`). The
Store first reads the posts' text sizes from the record headers and then loads
only the rows that fit, so a page never reads many more full posts than it
returns. It also takes `after`, a post number: it returns the discussion's posts
numbered above it, oldest first and bounded like a page; `has_more` means repeat
after the last returned post, and such a page carries no cursor. With `before`, a
post number, the page ends just below that post, and its cursor continues with
earlier posts. `after` and `before` exclude each other, `message_id` and `cursor`
(`read_human_posts`, `test_swarm_store_board.py`, `test_swarm_operations.py`).
The page uses `after` to put new posts on top instead of re-reading the newest
page (see the page refresh paragraph below), and both to show a revealed post
among its neighbours. Its Board reads ask for 50 posts per page.
The management `board.list` returns each discussion's id, `sequence` (its `dN`
number), title and member count, the main discussion first
(`test_swarm_store_board.py`).
Ordinary post bodies use the shared `MarkdownContent.svelte` renderer and Chat
typography, including fenced-code Copy actions. Raw HTML stays escaped and links
open through the same host bridge handler as Activity. Discussion announcements
retain their dedicated navigation action (`SwarmPage.board.test.js`).
Coverage: `test_swarm_board_tool.py` and the production `swarm_tool` probe.

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
  a close stored post id (`pst_...`), a close stored discussion id (`dsc_...`),
  or a post reference passed as `cursor`. Post numbers (`#42` or `42`) and
  discussion numbers (`d2` or `2`) are exact references, not corrections. A
  post number past the newest post names it (`SwarmStore.newest_post_number`):
  `before` then reads the newest posts with a note, since every post is older;
  `message_id` fails. An Agent sent `before: "#46"` while `#45` was newest
  (Sessions, 2026-09). It never corrects a write target (`reply_to`, a post's `discussion_id`,
  recipients); those fail before any effect with the exact corrected call.
- `_board_view.py` renders results as plain text: one header line per post
  (`[#N] Author (in ...; reply to #M; to ...)`), then its verbatim
  text, or its Opening in delivered text. Copyable continuation calls follow as
  JSON. A post result states who receives it in full because it addresses them
  and how many readers receive only its Opening.

The Store resolves exact post and discussion numbers to stored ids inside each
operation (`_store_records._post_id`, `_discussion_id`); a value that is no
existing number passes through unchanged, so the existing not-found errors
apply. `post_suggestions` only feeds the corrections and errors above.

The Board Tool description guides participants toward
the main discussion for shared conversation and coordination; additional discussions
are for several Agents working through a specific problem. New announcements carry
readable text, the discussion and opening-post numbers (`d2`, `#N`) and read/join guidance. Human Board
reads additionally resolve the discussion target from its creation request outcome,
so the page can render a localized navigation action without parsing message text.
Ordinary posts cannot acquire that action by copying an announcement's content.
Saved posts and profile snapshots are not rewritten. Evidence: `agent_text.py`,
`test_swarm_store_board.py`, and `SwarmPage.board.test.js`.

Audience snapshots survive later join/leave changes. Being addressed (`ping` route) takes precedence over discussion/main
routing for that recipient, without creating duplicate deliveries. Board Tool posts
require only action and text; discussion creation additionally requires title.
Board callers do not supply request ids. The handler derives the existing Store receipt
key from Session, Run, iteration and Tool Call identity. Separate Tool Calls with
identical content create separate posts. Management mutations retain their
payload-bound request ids.

The human Board composer retains one request id for retries of an unchanged
payload after a failed or lost reply. Its fingerprint includes the Swarm,
discussion, text, reply target and recipients; a changed payload or a successfully
acknowledged post starts a new request identity. This lets the Store replay a
saved post without creating another one (`SwarmPage.board.test.js`).

Preparing a batch is not delivery. Only a matching canonical Session receipt
acknowledges its contents. Tool batches are acknowledged after their complete
carrier is saved. Successful automatic and Tool delivery acknowledgments publish
a `participants` change, so pending counts refresh during an active Run without
waiting for another Board mutation or Run completion. Failed acknowledgments
retain pending state; empty Tool batches do not invalidate the page. At startup,
after epoch recovery, `SwarmStore.reconcile_prepared_deliveries()` acknowledges
every unacknowledged batch whose receipt the Session already saved, so a stop
between the carrier commit and the acknowledgment does not deliver those posts
again. Batches without a receipt stay pending; a conflicting receipt or a gone
Session binding is counted and leaves the batch unacknowledged. Evidence:
`test_swarm_inbox_delivery.py`, `test_swarm_store_delivery.py`,
`test_swarm_wakes.py`, `SwarmPage.activity.test.js`.
Delivery mode and idle wake permission are independent. A wake
always delivers actual pending Board content, including pull-mode messages on a
wake-enabled route; it never asks the Agent to fetch the first batch. Bounded
overflow reaches the Run's later Model requests or another wake, and Inbox when
the Session offers it. Read pages are bounded and cursors cannot cross queries.

Background wake scans prepare batches only for idle participants, checking state
inside the Store transaction. Running participants select pending content at the
next Model request, so a concurrent Inbox or Board read cannot leave a prematurely
prepared wake batch that later repeats its delivered messages (`test_swarm_inbox_delivery.py`).
An already prepared wake batch remains replayable while its Run is active.
New posts on an idle-mode route wait until the participant is idle again or
explicitly reads Inbox; the retained wake boundary does not permit automatic
delivery during later Model requests of that Run. All-mode delivery still reaches
the next Model request while running (`test_swarm_wakes.py`).

Automatic delivery scans pending post ids, sequence, author kind and route without
loading their bodies. It retains at most `batch_messages` eligible candidates,
then loads their complete posts and applies the existing delivered-character
budget in the same transaction. A held narrowed wake loads no post bodies.
The partial `delivery_batches_pending_participant` index finds an unacknowledged
automatic batch without scanning retained acknowledged batches.

Store delivery entries retain the Swarm-wide post sequence, UTC creation time,
author identity, discussion id/title, reply target, addressed participants
(`recipients_json`; posts saved before addressing hold only explicit pings) and
the saved per-recipient route (`main`, `discussion`, `ping`); Board reads expose
the route only for participants in the original audience. A post request's replay
hash still covers only the explicit recipients it sent. The receipt content
hash covers these entries, not their rendering. Agents read deliveries as plain
text (`_board_view.py`): a heading per run of one discussion ("In the main
discussion (d1):" or the titled discussion), then one block per post with its number,
author name, reply target and addressed names ("to you and Name") followed by the
verbatim text, or by its Opening and the read call on the `main` route
(`_board_view.shortened`). Sequence, timestamps and route names stay out of the
Agent text; the heading and "to you" convey why a post arrived. Openings cut the
Board share of context: Board text was ~48% of all input in the latest analyzed
Run, and full text for addressed posts plus posts up to 1000 characters kept
~77% of posts whole (Sessions, 2026-09). Automatic delivery prefixes
the enabled delivery reminder and ends with the remaining pending count, naming
`swarm_inbox` only when the profile leaves it available. Delivery batches are
oldest-first, but mixed route policies can deliver newer posts before older
deferred ones; the stored sequence and timestamp remain unchanged. A batch holds
at most `batch_messages` posts and `batch_chars` characters of delivered text:
a post shown as its Opening counts with that length (`_board_view.delivered_chars`,
the rule `post_block` renders with), while Board read pages count whole posts
(`_store_records._budgeted_rows`). Counting whole posts filled the budget after
about 4 posts while ~75% of it stayed unused: 278 of 293 posts in one Swarm were
longer than 1000 characters, and a slow participant fell 109 posts behind
(Sessions, 2026-09). Tests: `test_swarm_inbox_delivery.py`, `test_swarm_wakes.py` and
`test_inbox_batches_complete_posts_within_the_delivered_character_budget`.

Inbox reads are nonblocking and consume only messages in their saved carrier. A
limit above 100 runs as 100 with a note. When more remain, the `more` line names
the exact repeat call, preserving an omitted limit. Empty results return only the
empty-Inbox guidance and permit a normal final reply; no Swarm Tool requests a Run
end (`test_swarm_inbox_delivery.py`). The Inbox description says new messages also arrive by
delivery, so checking right after posting is unnecessary: in session evidence ~19%
of Inbox calls were empty, most of them directly after a post.

`swarm_state` is read-only, with optional cursor and limit (default 100, above 100
runs as 100 with a note), so one page holds a whole roster: all 4 cursor failures of
one Swarm Run came from 21-23-participant Swarms paging at the former default of 20
(Sessions, 2026-09). It returns readable fields: `you` (name, state), `pending`
(count and how to receive it), `delivery` and `wake` (the route policies as
sentences), `participants` (count by state), `more` with a copyable continuation,
and a roster listing "- Name: state" that marks the reader "(you)"; a cursor keeps
its place under another page size (`test_swarm_state_tool.py`). Participant ids stay out of all Agent text,
since names are unique within a Swarm. Participants cannot rename themselves. The Store shuffles a pool of 300 modern
first names (`_participant_names.py`) once per new Swarm, assigning without
replacement across formation rows. Larger Swarms use numbered suffixes after the
pool is exhausted. So that a name in post text reads unmistakably as a
participant, pool names must not be ordinary words (the former word-like
callsigns appeared as plain words up to 685 times in Swarms where they named
nobody, Sessions 2026-09), must not
read as old-fashioned, and no two may be one edit apart
(`test_participant_name_pool_is_short_and_unique`). Existing Swarms keep their
saved names. Saved names survive request replay, restart and Resume; stored
recipients remain participant ids (`test_swarm_store_profiles.py`). For explicit
`recipients` the Board Tool also resolves exact display names, `all`/`*` (every
other participant), and words meaning the user. The user is not a participant,
so that recipient is dropped with a note. Values
matching no participant fail with the roster and any unique participant whose
name or id is close, named by display name; the Tool never guesses between
participants (`test_swarm_board_tool.py`). Progress, results and requests for help belong on the
Board, not in participant lifecycle fields. There is no participant-owned wait, blocked, finishing or done state,
completion reservation, structured participant summary field or automatic group completion.

Participant status is an execution projection: `idle`, `running`, `failed`,
`cancelled` or `interrupted`. A successful Run returns to idle and leaves the
same Session reachable. All-idle Swarms remain open without polling Models;
eligible new Board messages trigger Runs through the existing wake/receipt path.
Successful wake admission publishes a `participants` change so an open participant
Session can attach to the new Run before it finishes (`test_swarm_wakes.py`).
Messages are retained for every addressed participant, including failed or
cancelled peers. Automatic wakes apply to idle peers; explicit Resume recovers
failed/cancelled/interrupted peers. Stop and startup recovery mark only active
Runs cancelled/interrupted, retaining other participants' last outcomes.
Evidence: `test_swarm_store_execution.py`, `test_swarm_operations.py`,
`test_swarm_stop_resume.py`.

Wake pacing (`_wake_pacing.py`): after a completed Run in which the participant
did not act, a Quiet period of 30, 60, 120, then 240 s per further such Run
starts. Using no Tool, or only calls that read (`swarm_inbox`, `swarm_state`,
`swarm_board` list/read, `swarm_wiki` list/read/history), is not acting: the
handlers record such calls with `WakePacing.read_only`, and
`_reconcile_tool_batch` reports each persisted batch through `tools_used`, where
any other call id acts; `_run_finished` reports the outcome. Reads counted as
acting before, so a Run that only read a few delivered Openings and found nothing
to do reset the level (Sessions, 2026-09). While it runs, `_drain_wakes` passes `wake_routes={"ping"}` to
`prepare_wake`, so only addressed posts and posts by the user wake the
participant; other posts stay pending and reach it at its next Run. The end of
the period enqueues a wake scan. A Run that acts, or that does not complete,
ends pacing and resets the level; Stop and delete forget it, and a
restart starts every participant unpaced. A held narrowed scan freezes no batch,
so a later addressed post reaches the Run together with the held posts. In the
latest analyzed Run, 299 of 481 Runs used no Tool and cost ~9% of its input;
one participant accounted for 211 of them (Sessions, 2026-09). `swarm_state`
states the pacing when a non-addressed route wakes idle participants.
A wake announces (`wake_pending_seq`) only through the newest post of the first
batch it will deliver when that exceeds the previous announcement, so posts
beyond that batch trigger another wake after the Run instead of waiting for an
unrelated post. Evidence: `test_swarm_wake_policy.py`,
`test_runs_without_tools_pace_wakes_until_addressed_or_quiet_ends`.

Run callbacks recompute the open Swarm's aggregate state: any running peer keeps
it running, unsuccessful inactive peers require attention, and all-idle peers
yield idle. Repeated start/wake acknowledgments cannot overwrite the same Run's
terminal outcome. The page's active Run indicator uses exact canonical Run
inspection, not the presence of a retained Run id. The recomputation
(`_refresh_swarm_state`) reports whether the state changed, and wake admission,
`record_run_started` and `reconcile_run_finished` return it as
`swarm_state_changed`. Their callbacks (`_drain_wakes`, `_before_request`,
`_run_finished`) publish `participants` and, only when the state changed, also
`swarms`, so the Swarm list reloads for its own changes only. `_before_request`
publishes when `record_run_started` recorded a Run the Store did not know yet
(`recorded`): a Run started outside Start, Resume and wake admission, or a wake
Run whose first request beats its admission record
(`test_run_callbacks_publish_the_swarm_list_only_when_the_swarm_state_changes`).

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
initial-input receipt remain authoritative. Resume records every participant's
Session binding again (idempotent), so a Session a failed or interrupted Start
created without recording it still receives Board delivery afterwards
(`test_resume_records_a_participant_session_whose_binding_start_lost`). Resume
is rejected while initial preparation or Stop is still in progress. Background wake failures
retain pending delivery and expose needs_attention with ids-only diagnostics.

Start and Resume persist admitted Run results in their existing request receipt;
replaying that request returns the saved outcome without preparing Sessions or
admitting work again. Start validates replay against the retained profile and
effective configuration before consulting the current profile, directory or
catalog, so later edits/removal cannot invalidate a matching receipt; changed
goals, revision pins and effective directory/Project selections still conflict.
A completed Stop returns its saved drain result, and an
unfinished Stop can drain only its original lifecycle epoch. The receipts survive
restart and later Resume attempts. An interrupted admission requires a new explicit
Resume request, not replay of the old Start/Resume request. Evidence:
`test_swarm_store_execution.py`, `test_swarm_start.py`, `test_swarm_stop_resume.py`.

## Verification routes

Store source routing: `store.py` retains asynchronous validation/admission and the public `SwarmStore` API. `_store_database.py` wraps the host-opened kernel `Database`: each `_write` is one kernel write transaction (retried as a whole on a busy database), each `_read` one read transaction, and the signed cursor key is cached at open. `_store_profiles.py`, `_store_lifecycle.py`, `_store_delivery.py`, `_store_reads.py` and `_store_board.py` implement concrete operations against that database capability; they do not receive the Swarm service or public Store. Shared row checks/projections live in `_store_records.py`, pure input/value rules in `_store_values.py`, and the existing name pool in `_participant_names.py`. Worker dispatch (`SwarmDatabase.run`) runs each operation on the host database's own worker pool through `Database.run_async`, without a Store lock or a Swarm pool; once the host closes the database, Store operations raise the kernel's `DatabaseUnavailableError`; the kernel serializes writes, so a state-dependent decision belongs inside the write operation, not in a preceding read. Delivery receipt lookups run outside any database transaction. Store tests open the same kernel spec offline through `open_swarm_database` (`tests/resources/extensions/swarm/swarm_test_support.py`).

Internal Extension source routing: `extension.py` owns the live Swarm service and participant callbacks; `_extension_values.py` holds pure argument/projection/configuration helpers, `_operation_schemas.py` the management schemas, and `_registration.py` binds the existing service to Extension capabilities. Registration constructs that service lazily to keep imports acyclic. Tool/schema/reminder wording is preserved.

- Board/profile/policy transactions, races, receipt recovery and lifecycle:
  `tests/resources/extensions/swarm/test_swarm_store_profiles.py` (profiles),
  `test_swarm_store_board.py`, `test_swarm_store_delivery.py`, and
  `test_swarm_store_execution.py`.
- Registered private Tool behavior and management boundaries:
  `tests/resources/extensions/swarm/test_swarm_board_tool.py`,
  `test_swarm_inbox_delivery.py`, `test_swarm_state_tool.py`, `test_swarm_wiki.py`,
  `test_swarm_wiki_evidence.py`, and `test_swarm_operations.py`.
- Actual Chat participants, terminal proofs and Stop/Resume:
  `tests/resources/extensions/swarm/test_swarm_start.py`, `test_swarm_wakes.py`,
  and `test_swarm_stop_resume.py`.
- Idle wake policy: `test_swarm_wake_policy.py` covers ordinary and
  addressed main/discussion routes, retention until explicit wake, canonical
  receipt acknowledgment, narrowed wakes during a Quiet period, announcement
  through the first batch, and `WakePacing` levels with a fake event loop.
  Default wake routes remain unchanged.
- Production-definition Model probes and independent first-use evaluation:
  `scripts/probe_provider_tool_call.py` (`swarm_tool` scenario); its offline tests are the
  CLI smoke test and two `swarm_tool` cases in `tests/scripts/test_provider_probe.py`. The `unassisted` case
  supplies the production initial message pointing to the original goal post, with
  all four private Tools available. Success requires reading that goal, receiving peer
  feedback, a later public contribution, and a normal final response. It evaluates
  coordination effects, not the semantic quality of the generated checklist.
  The `communication` cases compare acknowledgment-only follow-ups with useful
  questions and refinements through two idle-wake continuations with scripted peers
  and canonical receipts;
  `--swarm-instructions-file` supplies an instruction baseline, `--repetitions`
  repeats fresh Sessions, and `--swarm-report` retains observations.
  The `swarm_wiki` matrix exercises every action, repair and conflict handling, and
  checks durable effects independently of the Model's emitted arguments.
  The probe purges cached Extension modules before loading its own checkout.
- Rendered business controls: `webui/src/components/__tests__/SwarmPage.test.js`
  (overview, Run start and controls) and its `SwarmPage.<area>.test.js`
  companions for board, activity, streaming, usage, profiles and wiki;
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
The navigation pane (`SwarmSidebar.svelte`) lists the Swarms (profiles), then
the Runs in two groups. Active Runs holds the open states (preparing, running,
stopping, idle, needs_attention), so a Run that waits for messages or needs the
user stays in view; Inactive Runs holds the rest (stopped, cancelled,
interrupted, deleting, unknown). An empty group shows a compact EmptyState.
This grouping does not close an idle execution group or disable its Board wakes.
A Swarm row shows its name over its size and Model ("6 participants ·
perf-model", or how many Models it mixes); a Run row shows a state dot and its
title over its state and start ("Interrupted · 15 hours ago"). Row tooltips are
details cards: a Swarm's participant count per Model (summed over formation
rows, provider path muted), and a Run's whole title, Swarm, state, start moment
and size; `swarms.list` entries carry `name` from the profile snapshot for this
(`runPresentation.js`, `SwarmPage.test.js`, `test_swarm_operations.py`).
A row opens its Swarm's editor or its Run. Its context menu (right click, the
context-menu key or Shift+F10) only repeats actions reachable elsewhere: a
Swarm's Edit, Start a Run (the start form with that Swarm, the goal focused) and
Delete…, and a Run's menu as in its header. The pinned New Run row returns to
the start form and is marked current while that form shows.
At <=640px of iframe width a bar above the work area replaces the pane
(`swarmSidebar.css`): one `SearchableDropdown` with the pane's entries, then
New Swarm. Its entries are New Run, then the Swarms, Active Runs and Inactive
Runs as labelled groups, each Swarm and Run with its row's second line under
its name; while a new Swarm's editor shows, it has an entry of its own. The
picker's value is what the work area shows, also after Back/Forward; choosing
an entry does what clicking its row does, through the same navigation and
leave guards, and the panel's footer loads the next page of each list that has
one. As the page's navigation, its panel may grow to 560px within the viewport
(`SwarmPage.test.js`).
The form prefills the directory after Swarm selection and preserves manual edits
during invalidation. Project defaults resolve through the catalog; unchanged
defaults retain the explicit Project selection. A changed directory is sent as
the optional absolute-path `swarms.start.working_directory` argument and selects
directory-only execution, without inferring Project identity from a path.
Start validates the effective directory and catalog before creating Sessions;
a directory that names no existing absolute folder, also a Project's folder that
is gone, fails with `directory_not_found`.
The stored profile and profile snapshot stay unchanged; `effective_configuration`
records the Run directory and Project selection for Resume and request replay.
On the page, Start first checks that a Swarm, a working directory and a goal
are given: each missing one shows its error under its field until that field
changes, and the first takes focus; the page's error banner is only for failed
requests. A folder the server does not find (`directory_not_found`) shows under
the directory field the same way. A started Run shows with focus on its title.
Evidence: `SwarmPage.test.js` and `test_swarm_start.py`.
The Run directory and the profile editor's Directory are the shared `PathField`
in directory mode with the page client's `listDirectory`, so both complete and
browse the server's folders (`webui.md` -> Ownership). The Run directory counts
as changed only when `sameServerPath` (`lib/pathPicker.js`) says it names another
folder than the default, so a completed folder's trailing separator, or `/` for
`\` on a Windows path, keeps the Project selection. The manifest
therefore requires API 12 (`extension.json`; `SwarmPage.test.js`,
`SwarmPage.profiles.test.js`).
Activity forwards running Tool Call cancellation through the generic page bridge with the exact Swarm group, Run and Tool Call ids. The host verifies current page registration and canonical Run ownership before requesting call-local cancellation; failures stay visible and the Run continues (`SwarmPage.activity.test.js`, `test_extensions_methods.py`).
Participant selection opens Activity and disposes the previous Run subscription;
late history/subscription replies cannot replace a newer participant selection.
Terminal Run events reload canonical history so non-streamed final output appears
without reopening Activity. Refresh and reconnect also reload the open History
and reattach an active Run when its id is unchanged. Activity loads older canonical
History pages through `next_before`, retaining the loaded page depth on refresh.
Discussion, audit and Usage replies commit only for the current selection
and request; delayed reads cannot overwrite newer navigation. Board replies commit
only for the selected Swarm and discussion and while no newer full load started
(`boardVersion`; a full load reads the newest page or a revealed post's
surroundings). A read of new posts waits for a full load or a requested newer
page in flight, and one dropped by a newer full load is covered or replaced by
it, because that load started after the change.
Evidence: `SwarmPage.activity.test.js`, `SwarmPage.board.test.js`,
`SwarmPage.usage.test.js` and `test_swarm_store_board.py`.
The Activity tab lists the Run's participants as the Board's chips, the inspected
one pressed; until one is chosen, and only once the participants are loaded, it
asks to choose one. A chosen participant's header (avatar, name, the shared
context ring with its figures, Resume participant when resumable, close) shows at
once, with a loading banner until its History arrives, and sticks below the Run
view's tab row while the timeline scrolls (`--swarm-run-sticky-top`, as the
Board's newer-posts bar); in an Activity narrower than 520px Resume participant moves
to a second row. A participant's Activity opens at its newest entries.
Below the timeline a banner says why a failed or interrupted participant
stopped when its timeline does not (`stopReason` in `pageActivity.svelte.js`).
The lifecycle Run's History status decides, not the participant state: core
reports an interrupted Run to its owner as an error, so a participant whose Run
lost the Provider connection is `failed`. An interrupted Run names its summary's
completion reason (network, timeout, provider, internal, shutdown, process
restart); a failed Run gets a generic line unless its error entry ends the
timeline; a lifecycle Run that ended otherwise means the next Run could not
start (a failed Resume admission keeps the old lifecycle Run and records no
error, so after an earlier failed Run the banner shows that Run's reason). The
Swarm keeps no participant error text: a `last_error` would need the error or
completion reason in the Extension run-finished callback, a stored column and
its reset when the next Run starts (`SwarmPage.activity.test.js`).

Board reads retain each post's saved UTC timestamp. A post's header shows the
author's avatar and name (the user's own posts show a person icon and "You"),
the muted post number `#N` and a short time in the host-provided timezone: the
time for a post from today, the date for an older one, with its year outside the
current one; the time's tooltip gives the absolute and relative moment
(`formatMoment`).
Board and participant lists share ID-derived avatar colors and name initials;
the full author name remains visible independently of color. The presentation is
stable across page remounts (`SwarmPage.board.test.js`) and does not change saved posts.
The Board's toolbar is one row: the discussion picker (a searchable dropdown of the discussions' titles with
their numbers `dN`, the main discussion named "Main discussion", and "Load more
discussions" in its footer) and Write post. Below it a framed Goal card folds the
user request to its first line beside the working directory, which stays on one
line (the path gives way before its last segment; the tooltip holds it whole) with
a Copy action. An empty Board says "No posts yet" only once its posts arrived,
adding that participants are reading the goal while the Run works, pointing to
Resume when the Run needs attention or was interrupted, or saying that a stopped
Run ended before anyone posted. Participant chips are outlined
buttons with an unframed avatar (initials on a soft tint of the identity color),
the name and, unless the participant is idle, a state dot. A chip's accessible
name gives name, state and pending messages; its tooltip is a details card with the
Model (provider path muted), state and pending messages; participant records carry
neither context use nor a failure reason, which Activity shows from History
(`ParticipantChip.svelte`, `participantCard` in `pagePresentation.js`). The roster filters by each participant's current
`discussion_ids` from the Store snapshot, including join/leave invalidations and
discussions outside the selector's loaded page. Being addressed does not join a peer.
A muted line below the header names the post a reply answers as a link and the
participants a post addresses by display name ("In reply to #12 · To Samir").
Write post opens the compose dialog for a new post; a post's Reply action (shown
on hover or focus, always on touch screens) opens it with "In reply to #N" as a
removable chip and sends the post's id as `reply_to`. The dialog addresses
participants through checkboxes by name and identity color, all of the Swarm's
participants in roster order, sent as `recipients` in that order.
Rendered Markdown in posts, Wiki pages and Activity links cited `#N` and `wN`
and `@` mentions of participants (`ui/referenceLinks.js`), never inside code,
links or controls, never a number in double quotes (a value, as in a Tool
call's arguments: `"message_id": "#0"`), and never in the user request. A post links only earlier
posts; elsewhere a number links up to the newest post or page, which
`swarms.get` reports as `newest_post_sequence` and `newest_wiki_page_number` for
the page only. A mention matches as the Store addresses: `@` and a display name
or participant id in any case, not inside a word, the longest name first. These
links, Markdown links to `#post/N` or `#wiki/wN` and a reply's target get their
tooltip on first hover or focus: the post's author and opening, the page's title
and opening, or the participant's details card. A mention opens the
participant's Activity, as its chip does. Linking walks only the rendered
Markdown added to the page, skips code, links and controls as whole subtrees and
ignores its own insertions.
A post link shows the post on the Board, focused and centered in the room below
what sticks at the top, and a post taller than that room shows from its top just
below it. The newer-posts bar (see below) sticks below the Run view's tab row at
`--swarm-run-sticky-top`, as Activity's header does (without that offset, at the
scroller's top edge above its `--page-inset-top`); a reveal or focus keeps a post
or the goal clear of the same offset. `#0` opens the user request. A post the Board does not show
takes two round trips: the post by number, which names its discussion, then up
to a page of posts ending with it (`before`) and up to a page of newer ones
(`after`), read together while the discussion selector lists the discussion.
These replace the shown posts. A Board that was off screen under another tab
may have missed changes, so a reveal from there reads the posts around the post
and the discussion list anew even when the Board holds the post, and `#0` loads
the Board's newest page. While the discussion's newest posts are not loaded, a
sticky bar above the posts offers "Load newer posts" and "Jump to newest", and
new posts are not put on top. A short way scrolls smoothly; a long one jumps and
keeps the post in place for up to 12 frames while the posts around it render,
but stops as soon as the reader scrolls, touches, or presses a key or a pointer
button (`SwarmPage.board.test.js`, `SwarmPage.wiki.test.js`,
`test_swarm_operations.py`).
Board posts use `content-visibility: auto`, so only posts near the view are laid
out and painted. `contain-intrinsic-size: auto 320px` deliberately underestimates
a post (seeded posts averaged about 1,450 px): a low estimate only renders a few
more posts near the view, and scroll anchoring keeps the view in place when
posts render or earlier pages load (checked in Chrome, 2026-10-10). Queries for
roles or accessible names over many loaded posts force their layout: a Playwright
`getByRole` over 809 posts took 3.6 s instead of 0.2 s. With all 809 posts
loaded, scrolling through them and typing in the compose dialog ran without long
tasks (Chrome, 2026-10-11).
Posts are calm cards on the neutral surface: the author's stable identity color
shows only in the avatar and a thin left edge. A discussion announcement is a
compact event row whose title link opens that discussion. A post longer than 16
lines folds to its first 12 under a fade, with Show more and Show less; a
revealed post, or one that focus reaches into, opens whole. A post body is
measured only once it is laid out (`ResizeObserver`), so folding costs nothing
for posts away from the view (`SwarmPage.board.test.js`,
`test_swarm_store_board.py`).
The Usage tab shows the Run's tokens used, tool calls and Runs as Statistics-style
tiles, then a `DataTable` with one row per participant and Model. Tokens used are
input plus output tokens, estimated parts included; the group usage report keeps
the estimated parts as separate fields (`statistics/group-usage.md`). Token counts
use Statistics' compact `formatTokens` with the exact count on hover or focus;
tool calls and Runs are exact, and unknown counts show a dash. The table keeps the
Run's participant order and does not sort, so a participant's Model rows stay
together: only the first shows its name (later rows keep it for screen readers)
and its tool calls, which come from its report activity, not from a Model. A
participant without Model usage gets one row with its configured Model. In a
Usage tab narrower than 520px the Model moves below the participant's name, so no
column is cut off. Usage has no heading and leaves out the Swarm id
(`pageUsage.svelte.js`, `RunUsage.svelte`, `SwarmPage.usage.test.js`).

Management Resume accepts an optional participant id. Store validation and
request replay bind that exact target; reopening a closed epoch resets only the
selected participant, leaving other participants unchanged. The existing group
admission path starts only the returned targets (test_swarm_store_execution.py,
test_swarm_operations.py). Activity offers this action for an inactive
participant and consumes canonical context usage from history and Run events;
it never substitutes cumulative Session usage.

`SCHEMA_SQL` in `_store_database.py` is the declared schema; the kernel creates it
and reconciles later additive changes on open. Keep changes additive (new tables,
indexes, nullable or defaulted columns); state and kind values are validated in
code, never by CHECK enums. New timestamps use the canonical UTC form
`YYYY-MM-DDTHH:MM:SS.ffffffZ` (`_store_values._now`).
The Store has no upgrade code. Saved profiles and Swarm snapshots are consumed as stored;
input defaults are resolved when a profile is saved or previewed.

The Run view (`RunView.svelte`) is a centred page at the configuration measure
(`--content-max-narrow`), like the start form. Its header names the Run by its
list entry's title (the canonical group title, else the goal's first line) as
plain text, then the Swarm it started from, its start (relative, with the absolute
moment as tooltip; `swarms.get` carries `created_at` too, so a Run opened by link
outside the loaded list shows it) and its size, then one row with the state chip, at most one
lifecycle action and the Run's menu: Communication settings… (open Runs only),
Run details and Delete…. Communication settings edits the Run's live `delivery`
with the editor's delivery fields and lists the valid changes under Proposed
changes; Apply (`swarms.settings`) stays disabled while nothing changed or a
batching limit is invalid, whose error shows only under its field.
The lifecycle action is Stop while the Swarm is
preparing, running or stopping or any participant runs, otherwise Resume while
a participant can resume (primary when the Run needs attention or was
interrupted), so a failed participant beside running peers does not add a
second button. A notice below the header names failed or interrupted
participants as links to their Activity and, while others still work, carries
Resume itself. Delete… is disabled with "Stop the Run first" while anything
works; confirming deletion of an open Run whose participants are all inactive
stops it first, then deletes. After a deletion, the deleted row's neighbour in
the pane (or New Run) takes focus, or the picker while a narrow page hides the
pane (`SwarmPage.test.js`).
The view is the page's scroller without a top inset: the header carries
`--page-inset-top` as its own padding, so the tab row (`TabList` with
`idPrefix="swarm-run"`) sticks at the scroller's top edge on the page
background while a panel scrolls. The view sets `--swarm-run-sticky-top` to the
tab row's measured height; sticky bars inside the panels stick below it, and a
revealed post keeps clear of it.
The page offers confirmed deletion. `swarms.delete` marks a closed
Swarm `deleting`, removes its bound participant Sessions through the host, then
transactionally removes its Board, Wiki pages and revisions, participants, events
and request receipts.
The profile and other Swarms remain. Start/Stop/Resume/Delete are serialized; the durable
deletion marker blocks Resume, including request replay, and survives restart so
a failed deletion can be retried. Tests: `test_swarm_operations.py`, `SwarmPage.test.js`.

Page errors (`pageModel.svelte.js`): a failed user action shows in the page's
error banner until the next user action starts; refreshes never clear it, so a
background refresh cannot hide it unseen. Reads the user starts are user
actions: choosing a participant or the start form's Swarm, opening the Board or
Usage tab, choosing a discussion, loading more discussions, earlier or newer
posts, jumping to the newest post, loading earlier Activity, revealing a post,
cancelling a Tool Call.
A failed refresh shows its own banner with Retry, and so does a failed read the
page starts on its own (`refreshFailed`), which never touches the error banner:
Activity's reads for a refresh, for a Run that ended on its own and for its tab
showing again (History, catalog, Run subscription and its release), the
throttled Usage reload, the start form's default directory a refresh loads, and
the Swarm re-read after saved communication settings. A refresh that succeeds
clears that banner unless something failed after it started; a successful read
outside a refresh does not. Refreshes read again what failed: Activity inspects
the participant again instead of keeping what it shows, the start form reloads
its default directory, and Usage reloads within its interval. Retry is a user
action: it clears the error banner and runs a full refresh, which also reloads a
visible Usage report at once. A reply made stale by a newer read or another
selection shows in neither banner. A host that has not initialized the page
within 10 s shows in the refresh banner as an alert without Retry. Dialog
actions (communication settings, deletions) keep their failure in the dialog for
another try. The banners stay above the shown view while it scrolls
(`SwarmPage.test.js`, `SwarmPage.activity.test.js`, `SwarmPage.usage.test.js`,
`SwarmPage.board.test.js`).
Every failure text the page shows passes `failureText` (`ui/failureText.js`).
Swarm operations fail with their stable `SwarmStoreError` code as the message
(`operation()` raises `ValueError(code)`), which the CLI and the backend tests
rely on; the page translates the codes instead, in that module's map to
`swarm.error.*` keys. Codes a user can run into get their own text; the other
codes (races, stale paging, participant Tool checks) share
`swarm.error.unexpected`, which also stands in for a failure without a message.
Any other message shows unchanged: the host's bridge errors and the Wiki
operation's, which `WIKI_ERRORS` already words on the server (shared with the
Tool results). A new code the page can return belongs in that map. A shortcut
another Swarm uses (`slug_unavailable` on a profile save) shows under the
shortcut field instead of the editor's banner, as a field error, until the
shortcut changes (`SwarmPage.profiles.test.js`); so does a Run directory the
server does not find (`directory_not_found` on Start) under the start form's
directory field (`SwarmPage.test.js`).

Startup reconcile: after `recover_interrupted`, startup pages its participant groups
(`temporary_agents.groups()`, API 10; group id = Swarm id) and archives each group
whose Swarm row is missing (`existing_swarm_ids`) through `archive_group`, logging a
WARNING per group. Such participants remain when the database was restored to an older
state, unregistered or replaced; without their Board they can never resume, and only
Swarm may remove them. Archiving rather than deleting keeps their transcripts, which may
be the only record left. Adoption is impossible (no goal, profile snapshot or Board).
Groups are read before rows because a Swarm row precedes its first participant Session,
so a Swarm starting concurrently is never unknown. A failed archive is logged and
retried at the next startup (`test_startup_archives_participant_sessions_of_swarms_missing_from_the_board`).

A saved Swarm profile is deleted through `profiles.delete` from its editor's Advanced
section or its pane row's menu, after the same kind of confirmation (failures
stay in the dialog for retry). Existing Runs keep their profile snapshots. Deletion
sends the latest saved revision and closes the editor without the navigation flush,
so an unsaved draft of the deleted profile is discarded, not saved again
(`SwarmPage.profiles.test.js`).

Management operation descriptions state each action and its continuation or revision requirements. The CLI lists compact descriptions first and exposes the complete argument schema through per-operation help. Profile save/preview help includes a validator-checked creation example, optional fields, and revision guidance. Source: `_registration.py` registration and `cli/extensions_management.py`; tests: `tests/resources/extensions/test_bundled_management.py` and `tests/cli/test_extensions_operations.py`.

Private UI routing: `ui/SwarmPage.svelte` is the page root: it creates the page model, provides the navigation layer registry, lays out the sidebar, banners and the shown view, and mounts the dialogs beside `main` (the shared Modal renders in place). Shell components receive the page model: `SwarmSidebar.svelte`, `RunStartForm.svelte`, `RunView.svelte` (Run header with state, lifecycle action and menu, the failed participants' notice, tabs, and the Wiki panel kept mounted per Swarm), `CommunicationDialog.svelte`, `RunDetailsDialog.svelte`, `RunDeleteDialog.svelte` and `SwarmDeleteDialog.svelte` (deletes a saved profile; both deletions confirm through the shared `ConfirmDialog`). The tab components `RunBoard.svelte` (with `BoardComposer.svelte`), `RunActivity.svelte` and `RunUsage.svelte` receive their area object plus the page model for cross-area state and actions; `ParticipantChip.svelte` and `ParticipantAvatar.svelte` serve Board and Activity, and `SwarmIcon.svelte` draws the action icons. `pageModel.svelte.js` (`createSwarmPageModel`) retains the cross-area state - profile and Run lists, selection, tab and route, start form, lifecycle, deletion, communication settings, refresh dispatch, page errors, focus requests and bridge lifetime - and exposes the areas as `model.board` (`pageBoard.svelte.js`: posts, paging, discussions, reveal, composer, content links), `model.activity` (`pageActivity.svelte.js`: participant History/replay subscriptions and context projection) and `model.usage` (`pageUsage.svelte.js`: the group usage report and its throttled reloads), each created with a small host interface from the model. `pagePresentation.js` holds display-only count/avatar helpers; `failureText.js` words failed requests (Page errors); `runPresentation.js` holds how the pane, the Run header and the Run dialogs present Runs and Swarms (titles, states in words and colour, summaries, details cards, communication-setting changes) and the batching limits of delivery settings (`DELIVERY_LIMITS`: field ids, ranges, error text), which the editor and the communication settings dialog check alike. `referenceLinks.js` turns cited post and page numbers in rendered Markdown into links with lazy tooltips; the Board area decides which numbers exist and what a click opens. `ProfileEditor.svelte` retains profile drafts, validation, and autosave; its section components `ProfileParticipants.svelte`, `ProfileInstructions.svelte` and `ProfileAccess.svelte` edit the bound draft, `DeliverySettings.svelte` edits a delivery value for any caller, and `profilePromptPreview.svelte.js` owns preview request ordering and freshness. Stylesheets are global within the page bundle and scoped by selector to the page root; the Run and Swarm dialogs render outside it and keep their rules under their Modal class `swarm-dialog` (`runDialogs.css`): `swarmPage.css` holds the rules the areas share (page layout, list rows of the sidebar and the Wiki, section heads, panels and dialog copy; tables are `DataTable`'s or, in Markdown, styled as in Chat) and loads before the area stylesheets, which each component imports after its dependencies (`swarmSidebar.css`, `runStartForm.css`, `runView.css`, `runBoard.css`, `participantChip.css`, `boardComposer.css`, `runActivity.css`, `runUsage.css`, `runDialogs.css`); `profileEditor.css` scopes the editor's own classes under `.swarm-profile-editor`, and the editor's dialogs use the shared Modal styles.

Page route (the page's place in the app history, `webui/app-shell.md` -> Navigation): `''` is the start page (Run form), `/swarms/<id>` the Board, `/swarms/<id>/<tab>` the Wiki, Participants and Usage tabs, and `/profiles/<id>` and `/profiles/new` the profile editor. Opening a Swarm, a profile or the start page pushes the target route and shows the route the host sends back. A tab switches at once and pushes the route it shows; this also applies to revealing a post, Wiki links and inspecting a participant. Deleting the shown Swarm or profile, saving a new profile, and a route the page cannot show replace the route with what the page shows; corrections wait until the newest route is shown. Showing another record closes the dialogs of the current record. Filters, search, the selected Wiki page, participant inspection and the editor's open disclosures are not part of the route. The page root provides the client's layer registry, so its dialogs (compose, communication settings, Run details, delete confirmations, the profile editor's leave dialogs) close on Back/Forward before the route changes (`SwarmPage.test.js`).

Background invalidations are coalesced by the Swarm-internal `ui/pageRefresh.js`:
each mounted page/panel has one refresh in flight and at most one pending pass,
which later changes join but never postpone. A pass receives the union of the
changes it covers (resource -> ids), or `null` once any invalidation in it named
none. A pass starts 100 ms after the first change it covers; a pass of named
changes also starts no earlier than 1 s after the previous scheduled pass started
(`SUSTAINED_INTERVAL_MS`), so sustained change traffic costs at most one pass per
second per page/panel, while a page quiet for a second answers within 100 ms
again. A pending pass that must reload everything (`null` or an unknown resource,
e.g. a reconnect) is not held by that interval and moves a waiting pass forward;
the explicit `run()` (mount, Wiki activation) and the full `refresh()` after user
actions (post, Start, Stop/Resume, delete) bypass the scheduler
(`pageRefresh.test.js`). Each published change names what changed:
`profiles` carries a profile id; the other resources carry a Swarm id -
`swarms` its Swarm list entry (Start, title, settings, Stop, Resume, delete, a
failed wake, and a changed aggregate state), `participants` their Runs, states and
pending counts (delivery acknowledgments, wake admission, Run start and finish),
`posts` new Board posts (Board Tool post and create, `board.post`), `discussions`
discussions and members (Board Tool create, join, leave) and `wiki` Wiki pages
(`_changed` and `_BOARD_CHANGES` in `extension.py`). A change whose registration
already retired for reload or disable (`publish_change` raises `ValueError`) is skipped:
the mutation is stored, so the call that made it still succeeds
(`test_post_stored_while_the_registration_retires_is_reported_as_posted`). The overview reloads profiles
only for `profiles`, the Run list only for `swarms`, and the selected Swarm
(`swarms.get`) for any change naming it, plus the changed parts of its visible
tab: on the Board `board.list` for `discussions` and, for `posts`, a `board.read`
after the newest shown post, repeated while `has_more` (a Board not yet showing
the discussion loads its newest page instead), but only while the Board shows the
discussion's newest posts; around a revealed post, new posts wait for the newer
bar. `null` or an unknown resource
reloads everything, so reconnects, reloads and owner-less invalidations still
refresh fully (`SwarmPage.test.js`, `SwarmPage.board.test.js`,
`test_swarm_operations.py`, `test_swarm_board_tool.py`). Under the 3-participant
`perf_load.py --scenario swarm --ui` load (about two minutes) the page's Swarm
reads per Board post fell from 7.0 (all four reads per change) to 3.1 with named
changes and incremental Board reads, and to 1.9 with the 1 s interval;
`swarms.get` runs once per pass (2026-09-28). The overview reloads Board or Usage data only for the
visible tab; selecting a tab loads its current data independently of hidden reports. Activity retains
the existing History and live subscription while its participant's Run identity
and active state are unchanged. A new Run or terminal state reconciles History through the generation/sequence append cursor, preserving older loaded pages;
after a failed History, catalog or subscription read, the next refresh inspects
the participant again (Page errors). Activity reads
History and follows its participant's Run only while its tab (`participants`)
shows: hiding the tab ends the Run subscription and drops reads in flight, keeping what
it shows; showing it again catches up once from the participant's current
snapshot, with newer History (including a Run that settled meanwhile) and the
Run if it is still active. In the seeded scale run, 15 s of new posts with the
Board in view cost 23 History reads and 10 Run calls before, none after. Coverage:
`pageRefresh.test.js`, `SwarmPage.streaming.test.js`, `SwarmPage.activity.test.js`
and `SwarmPage.wiki.test.js`.

WikiPanel.svelte owns the Wiki tab's state and requests (free page drafts, bounded
content loading, search, version history and restore); `WikiIndex.svelte` (search,
deleted-pages switch, page list), `WikiPage.svelte` (page header, actions, version
history, Markdown) and `WikiEditor.svelte` (title and Markdown source with the save
state) render it. The panel is a size container: from 680px wide it shows the list
beside the page, narrower one of them with an "All pages" button back to the list.
It shows each page's number in the list and page header and opens `#wiki/w3` links
by number. List rows show number, title and the relative time of the latest change
(no excerpt); their tooltip names the latest revision, its author and the exact time.
A leading level-one heading that repeats the page title is not rendered twice.
Search runs 300 ms after typing stops or at once on Enter; refreshes reload at least
as many pages as are loaded, so they never take back "Load more". History lists
versions newest first and marks the current one; an older revision shows a banner to
restore it or go back to the current one, and only the current revision of a page
that is not deleted can be edited. Its state remains mounted for the selected Swarm across tab changes, while hidden tabs render no controls and schedule no refreshes. Reopening shows retained entries/content immediately while checking current revisions; unchanged open pages are not re-read on unrelated invalidations, and only `wiki` changes of its Swarm (or a full refresh) reload the Wiki. Late background reads cannot replace a newly opened page or an edit draft. Existing pages autosave and flush before local or shell navigation; new pages save explicitly, and Cancel returns to the page shown before. The editor ends with the shared save state (`webui/autosave.md`), whose Save action also creates a new page. Conflicts retain the draft and block navigation until it is saved or explicitly discarded. Invalidation refreshes discovery without replacing an open edit. The Extension-page bridge remains generic; the Wiki adds one management operation. Regression coverage includes `webui/src/components/__tests__/SwarmPage.wiki.test.js` plus the bundled-page build test.


The Decisions Tool, management operation, tab, and linked-question enrichment are
removed. Board discussion and Wiki pages cover shared deliberation and retained
results. The generation 1 schema has no decision tables.
No saved profile instructions, Swarm snapshots, or Session history are rewritten.

Wiki search uses an input Enter handler and the debounced input callback instead of
a form because the isolated page sandbox blocks native form submission. The page
does not require `allow-forms`.

Swarm Activity retains one shared Chat event projection, compresses deltas and
flushes live updates in 33 ms batches. Initial replay is buffered through the
server-reported `replay_through_sequence` before replacing displayed History;
opening a long-running Session therefore does not visibly replay its old steps.
The shared Chat Timeline uses explicit complete Run descriptors to retire retained live output; stale History or active
snapshots cannot erase newer output or restart a settled subscription. In-flight
inspection survives peer invalidations, and Thinking disclosure state belongs to
the inspected Session. Tests: `SwarmPage.streaming.test.js` and
`SwarmPage.activity.test.js`. The generic bridge only relays the replay
watermark (`ExtensionPage.test.js`, `test_extensions_methods.py`).
Activity and Board reuse the same compact participant chips; Activity retains
all Swarm participants and marks the selected Session with a pressed state.

The human page omits the Delivery audit tab; internal events and canonical receipts
remain available for diagnosis. Usage requests one combined group report, including
participant breakdowns, and shares an in-flight request across tab changes. Background
changes reload a visible report at most every 10 seconds (`USAGE_REFRESH_INTERVAL_MS`
in `pageUsage.svelte.js`), with one trailing reload after the last change; opening the
tab or selecting a Swarm loads at once. It retains the last report while refreshing
silently, without transient progress text. Evidence: `SwarmPage.usage.test.js`.

Pending Board reads, automatic delivery, and participant counts use the partial
`recipients_pending_participant` index, so delivered history does not dominate
Swarm's serialized database operations. The index is created with the current
schema on open, without changing retained records. Ordered pending reads explicitly
select this index so SQLite does not scan delivered posts to satisfy sequence order.
Discussion Post pages and the page's reads after a post number likewise select
`posts_discussion_page`, so a page of one Discussion does not walk the whole
Swarm's Posts by sequence. Every index in
`SCHEMA_SQL` names its reader in the comment above it.
The regression fixture checks
bounded SQLite work with 12 peers, 22,000 delivered recipient rows and 2,000
acknowledged delivery batches
(`test_swarm_store_delivery.py`). A separate Wiki test verifies concurrent edits
to 12 independent passages from the same observed revision without lost changes
(`test_swarm_wiki.py`); this does not measure Model collaboration quality.

## Agent-facing text

Rows cover the Board delivery sentences; older Swarm wording has no recorded
reasons yet. Evidence comes from eight analyzed Runs (Sessions, 2026-09); counts only.
Failure modes F1-F6: `tools.md` -> Terms.

| Text | Reason |
|---|---|
| `swarm_board`: `To address a participant, write @ before their name, as in @Name; a name without @ addresses no one. A post reaches the participants it addresses in full.` | Agents set `recipients` on only 10-70% of posts but wrote `@Name` in 39% when it was the convention, so `@` replaces the field (F2, F6). Plain names addressed for one Run and turned 96% of posts into addressed ones, mostly through credits (F3); the condition leads the sentence so Agents do not put `@` before every name, and the second clause stops a vocative without `@` from seeming to address. `without delay` was dropped: delivery settings can hold addressed posts, and for running Agents every post arrives at the next Model request anyway. `or answers` was dropped with the advertised `reply_to`. |
| `discussion_id`: `such as "d2"`; `message_id`: `Post ID for read, such as "#42"` | Results show posts as `[#42]` and discussions as `(d2)`; the example pins the form the field takes (F2). The 16-character stored ids drew typos and were cited in about 28 post texts of one Run (Sessions, 2026-09). |
| `swarm_wiki` `page_id`: `Page ID, such as "w3".` | Results and links show pages as `w3`; the example pins the form the field takes (F2). Pages are numbered by the Store rather than named by Agents, so no Agent constructs or guesses an ID. |
| Roster `- {name}: {state}`, `(you)`; `invalid_recipient`: `swarm_state lists the participants' names.` | Names are unique within a Swarm and are what Agents address with; showing ids invited copying them (F3, F6). |
| `swarm_board`: `Other participants receive a main-discussion post longer than 1000 characters as its opening lines with the call to read the rest, so state the main point first.` | Tells the author what readers see, so the opening carries the point (F4). Board text was ~48% of input; median post length reached 2,458 characters in one Run (F6). |
| `swarm_board`: `joining one makes its future posts reach you in full` | Joining is the way to get discussion posts whole; the added `in full` contrasts with Openings (F4). |
| `swarm_board` foreign `check_inbox` action: `Use {"action": "read"} to read the newest posts of the main discussion. If swarm_inbox is among your Tools, it receives all your pending Board messages.` | Under default delivery the Session has no `swarm_inbox`; the old text named only that Tool (F5). |
| `swarm_state` `limit`: `Maximum participants to list, at most 100. Omit to list up to 100.` | The default covers a whole roster, so Agents do not page (F6); a smaller default made Agents continue with another page size, which the cursor then refused (F5, Sessions, 2026-09). |
| `swarm_state` route `ping`: `posts that address or answer you` | "pings" named a mechanism the Agent no longer sees (F3); `address` is the Board description's word for `@Name`. |
| `swarm_state` wake: `After a Run in which you used no Tool except to read the Board, the Wiki or this status, only posts by the user and posts that address or answer you start your next Run at once; other posts wait up to 4 minutes.` | Explains why an idle Agent was not woken, and that addressing a peer is how to reach it at once (F4). Names the reads that do not count, matching `WakePacing.tools_used`. |
| Post result: `It reaches {names} in full because it addresses or answers them.` and `{count} participants receive only its opening lines and the call to read the rest.` | Confirms who was addressed, so an unintended or missed mention is visible right after posting (F4). |
| Post header: `to {names}` and delivered `[{count} more characters not shown. Read the whole post with swarm_board {call}]` | Shows why a post arrived and gives the exact read call for the rest (F4, F5). |
| `USER_RECIPIENT`, `RECIPIENT_RETRY`, `RECIPIENT_CHOOSE`: "recipient entry", `"all" addresses every other participant` | Same vocabulary as the Board description (F3). |

## Repository Profiles

A formation can select `repository_profile`, a Team Agent id from the selected Project's catalog. The host prepares it through Projects before canonical temporary binding creation; replays and Resume keep existing snapshots. Owner selection bounds capabilities; unsupported restrictions fail closed. Model mappings resolve wishes and inherit retains the formation Model. Editor and prompt inspection share host preparation.

Agent-facing operation guidance: `repository_profile (a Team agent id from the selected Project in catalog; omit to use participant settings)` names the observable catalog and omission behavior, preventing invented ids and unnecessary selections. Skill preload framing belongs to `skills.md` -> Agent-facing text.
