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
  `ui/i18n.js` holds the page's `swarm.*` English text; `ui/main.js` registers it
  with the WebUI catalog before mounting, and `SwarmPage.support.js` does the same
  for the page tests. The WebUI i18n catalog guard checks these keys like core
  keys, within this page only.
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

A profile's Tool and Skill selection applies as saved, also when the Swarm works in a Project whose Tool or Skill whitelist is narrower; the Project supplies directory, Skills and context (`projects.md`). Profiles expose all four private Swarm Tools under Tools & Skills, using the existing `tool_access.denied` policy for individual switches. All/None also enables/disables the current private set. The public Tool catalog remains unchanged; the Swarm editor appends owner-private catalog entries. Choices apply to future executions, while Resume retains the saved snapshot. Disabling a Tool does not remove its human tab or stored content and does not change independently configured Board delivery/wakes. Preview and actual Model definitions respect the same denials (`test_runtime_extension_host.py`, `test_swarm_start.py`, `SwarmPage.profiles.test.js`).
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
missing participants from the saved snapshot. The editor's Overview Compaction
section reuses the shared `CompactionPolicyEditor`; turning the override on starts
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
repairs; ambiguous candidates and literal marker differences are terminal. The
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
The management `board.read` (human projection, not the Tool) also takes `after`,
a post number: it returns the discussion's posts numbered above it, oldest first
and bounded like a page (`limit`, `batch_chars`); `has_more` means repeat after
the last returned post, and such a page carries no cursor. `after` excludes
`message_id` and `cursor` (`read_human_posts`, `test_swarm_store_board.py`,
`test_swarm_operations.py`). The page uses it to put new posts on top instead of
re-reading the newest page (see the page refresh paragraph below).
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

Preparing a batch is not delivery. Only a matching canonical Session receipt
acknowledges its contents. Tool batches are acknowledged after their complete
carrier is saved. Successful automatic and Tool delivery acknowledgments publish
a `participants` change, so pending counts refresh during an active Run without
waiting for another Board mutation or Run completion. Failed acknowledgments
retain pending state; empty Tool batches do not invalidate the page. Evidence:
`test_swarm_inbox_delivery.py`, `test_swarm_wakes.py`, `SwarmPage.activity.test.js`.
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
initial-input receipt remain authoritative. Resume is rejected while initial
preparation or Stop is still in progress. Background wake failures
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
The sidebar groups preparing/running/stopping records as Active runs;
all other states, including idle and needs_attention, appear under Inactive runs.
This presentation does not close an idle execution group or disable its Board wakes.
Sidebar tooltips show a Swarm's participant count per Model (summed over formation
rows) and a Run's title, Swarm name and participant count; `swarms.list` entries
carry `name` from the profile snapshot for this (`SwarmPage.test.js`,
`test_swarm_operations.py`).
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
Evidence: `SwarmPage.test.js` and `test_swarm_start.py`.
Activity forwards running Tool Call cancellation through the generic page bridge with the exact Swarm group, Run and Tool Call ids. The host verifies current page registration and canonical Run ownership before requesting call-local cancellation; failures stay visible and the Run continues (`SwarmPage.activity.test.js`, `test_extensions_methods.py`).
Participant selection opens Activity and disposes the previous Run subscription;
late history/subscription replies cannot replace a newer participant selection.
Terminal Run events reload canonical history so non-streamed final output appears
without reopening Activity. Refresh and reconnect also reload the open History
and reattach an active Run when its id is unchanged. Activity loads older canonical
History pages through `next_before`, retaining the loaded page depth on refresh.
Discussion, audit and Usage replies commit only for the current selection
and request; delayed reads cannot overwrite newer navigation. Board replies commit
only for the selected Swarm and discussion and while no newer full load of the
newest page started (`boardVersion`); a read of newer posts waits for a full load
in flight, and one dropped by a newer full load is covered by it, because that
load started after the change.
Evidence: `SwarmPage.activity.test.js`, `SwarmPage.board.test.js`,
`SwarmPage.usage.test.js` and `test_swarm_store_board.py`.

Board reads retain each post's saved UTC timestamp. The page formats it in the
host-provided timezone and separates the author/time header from the body.
Board and participant lists share ID-derived avatar colors and name initials;
the full author name remains visible independently of color. The presentation is
stable across page remounts (`SwarmPage.board.test.js`) and does not change saved posts.
The header shows the snapshot Swarm name and state. The Board holds the single
collapsible user request and working directory. Compact participant buttons show
names, identity colors and execution dots; Model/status/pending details are in
hover/focus tooltips. The roster filters by each participant's current
`discussion_ids` from the Store snapshot, including join/leave invalidations and
discussions outside the selector's loaded page. Being addressed does not join a peer.
Posts list their addressed participants by display name ("To: ..."); the compose
form's optional recipient field takes participant IDs. Each post header shows the
post number (`#N`) beside its time, a reply shows its target's number as a link, and the
compose form's reply field takes a post number.
Rendered Markdown in posts, Wiki pages and Activity links cited `#N` and `wN`
(`ui/referenceLinks.js`), never inside code, links or controls, and never in the
user request. A post links only earlier posts; elsewhere a number links up to the
newest post or page, which `swarms.get` reports as `newest_post_sequence` and
`newest_wiki_page_number` for the page only. A link's tooltip loads the post's
author and opening, or the page's title and opening, on first hover or focus.
A post link opens the post's discussion on the Board, loads earlier pages until the
post appears and scrolls to and focuses it; `#0` opens the user request
(`SwarmPage.board.test.js`, `SwarmPage.wiki.test.js`, `test_swarm_operations.py`).
Post backgrounds and left borders share the stable author color. The Swarm id
stays under Usage (`SwarmPage.board.test.js`, `test_swarm_store_board.py`).
Usage totals and participant Model rows abbreviate large counts with k/mio/mrd
and at most one locale-formatted decimal (`SwarmPage.usage.test.js`).
The Usage page combines measured and estimated input/output counts as "Tokens used"
at total and Model-row scope. Tool Calls come from each participant's canonical
report and span its Model rows once, including participants without Model usage.
This is a Swarm presentation choice; canonical usage remains separated
(`SwarmPage.svelte`, `SwarmPage.usage.test.js`).

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

The header offers one lifecycle action: Stop while the Swarm is preparing,
running or stopping or any participant runs, otherwise Resume, so a failed
participant beside running peers does not add a second button. Deletion is
disabled while that action is Stop; confirming deletion of an open Run whose
participants are all inactive stops it first, then deletes (`SwarmPage.test.js`).
The page offers confirmed deletion. `swarms.delete` marks a closed
Swarm `deleting`, removes its bound participant Sessions through the host, then
transactionally removes its Board, Wiki pages and revisions, participants, events
and request receipts.
The profile and other Swarms remain. Start/Stop/Resume/Delete are serialized; the durable
deletion marker blocks Resume, including request replay, and survives restart so
a failed deletion can be retried. Tests: `test_swarm_operations.py`, `SwarmPage.test.js`.

A saved Swarm profile is deleted through `profiles.delete` from its editor header or
beside the goal form's Swarm selector, after the same kind of confirmation (failures
stay in the dialog for retry). Existing Runs keep their profile snapshots. Deletion
sends the latest saved revision and closes the editor without the navigation flush,
so an unsaved draft of the deleted profile is discarded, not saved again
(`SwarmPage.profiles.test.js`).

Management operation descriptions state each action and its continuation or revision requirements. The CLI lists compact descriptions first and exposes the complete argument schema through per-operation help. Profile save/preview help includes a validator-checked creation example, optional fields, and revision guidance. Source: `_registration.py` registration and `cli/extensions_management.py`; tests: `tests/resources/extensions/test_bundled_management.py` and `tests/cli/test_extensions_operations.py`.

Private UI routing: `ui/SwarmPage.svelte` composes the page; `pageModel.svelte.js` retains its management state, request generations, Board/Usage loading, mutations, and bridge lifetime, while `pageActivity.svelte.js` owns participant History/replay subscriptions and context projection. `pagePresentation.js` holds display-only count/avatar helpers. `referenceLinks.js` turns cited post and page numbers in rendered Markdown into links with lazy tooltips; the model decides which numbers exist and what a click opens. `ProfileEditor.svelte` retains profile drafts, validation, and autosave; `profilePromptPreview.svelte.js` owns preview request ordering and freshness. Adjacent `swarmPage.css` and `profileEditor.css` scope styles to their page/editor surfaces, including portaled dialogs.

Page route (the page's place in the app history, `webui/app-shell.md` -> Navigation): `''` is the start page (Run form), `/swarms/<id>` the Board, `/swarms/<id>/<tab>` the Wiki, Participants and Usage tabs, and `/profiles/<id>` and `/profiles/new` the profile editor. Opening a Swarm, a profile or the start page pushes the target route and shows the route the host sends back. A tab switches at once and pushes the route it shows; this also applies to revealing a post, Wiki links and inspecting a participant. Deleting the shown Swarm or profile, saving a new profile, and a route the page cannot show replace the route with what the page shows; corrections wait until the newest route is shown. Showing another record closes the dialogs of the current record. Filters, search, the selected Wiki page, participant inspection and editor sections are not part of the route. The page root provides the client's layer registry, so its dialogs (compose, settings, profile snapshot, delete confirmations, the profile editor's dialog) close on Back/Forward before the route changes (`SwarmPage.test.js`).

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
(`_changed` and `_BOARD_CHANGES` in `extension.py`). The overview reloads profiles
only for `profiles`, the Run list only for `swarms`, and the selected Swarm
(`swarms.get`) for any change naming it, plus the changed parts of its visible
tab: on the Board `board.list` for `discussions` and, for `posts`, a `board.read`
after the newest shown post, repeated while `has_more` (a Board not yet showing
the discussion loads its newest page instead). `null` or an unknown resource
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
subscription failures remain retryable on a later invalidation. Coverage:
`pageRefresh.test.js`, `SwarmPage.streaming.test.js`, `SwarmPage.activity.test.js`
and `SwarmPage.wiki.test.js`.

WikiPanel.svelte shows each page's number in the list and page header and opens
`#wiki/w3` links by number. List rows show only number and title (no excerpt);
their tooltip names the latest revision and its author. It owns free page drafts, bounded content loading, search, version history and restore. Its state remains mounted for the selected Swarm across tab changes, while hidden tabs render no controls and schedule no refreshes. Reopening shows retained entries/content immediately while checking current revisions; unchanged open pages are not re-read on unrelated invalidations, and only `wiki` changes of its Swarm (or a full refresh) reload the Wiki. Late background reads cannot replace a newly opened page or an edit draft. Existing pages autosave and flush before local or shell navigation; new pages save explicitly. Conflicts retain the draft and block navigation until it is saved or explicitly discarded. Invalidation refreshes discovery without replacing an open edit. The Extension-page bridge remains generic; the Wiki adds one management operation. Regression coverage includes `webui/src/components/__tests__/SwarmPage.wiki.test.js` plus the bundled-page build test.


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
the inspected Session. Tests: `SwarmPage.streaming.test.js` and
`SwarmPage.activity.test.js`. The generic bridge only relays the replay
watermark (`ExtensionPage.test.js`, `test_extensions_methods.py`).
Activity and Board reuse the same compact participant chips; Activity retains
all Swarm participants and marks the selected Session with a pressed state.

The human page omits the Delivery audit tab; internal events and canonical receipts
remain available for diagnosis. Usage requests one combined group report, including
participant breakdowns, and shares an in-flight request across tab changes. Background
changes reload a visible report at most every 10 seconds (`USAGE_REFRESH_INTERVAL_MS`
in `pageModel.svelte.js`), with one trailing reload after the last change; opening the
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
bounded SQLite work with 12 peers and 22,000 delivered recipient rows
(`test_swarm_store_delivery.py`). A separate Wiki test verifies concurrent edits
to 12 independent passages from the same observed revision without lost changes
(`test_swarm_wiki.py`); this does not measure Model collaboration quality.

## Agent-facing text

Rows cover the Board delivery sentences; older Swarm wording has no recorded
reasons yet. Evidence comes from eight analyzed Runs (Sessions, 2026-09); counts only.

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
