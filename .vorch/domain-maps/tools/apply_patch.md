# Apply Patch Tool

Applies ordered V4A file operations. It replaces the archived `edit` and `write` Tools (see `edit.md` and `write.md`)
and also runs calls shaped for other harnesses' edit/write Tools.
Add File creation-or-replacement is a vBot extension to the V4A-style interface.
`core/tools/apply_patch.py` owns the in-memory plan, filesystem execution, and display metadata. Its internal `_patch_requests.py` turns other harnesses' argument shapes into canonical fields and parsed operations; `_patch_syntax.py` owns parsing (V4A, unified/git diffs, SEARCH/REPLACE blocks) and parsed operation values; `_patch_hunks.py` owns matching and applying one hunk or replacement to current text (including patch recoveries); `_patch_entries.py` owns entry snapshots, Delete/Move entry resolution, and entry renames; `_patch_report.py` owns the Model-facing result text; `_change_preview.py` owns bounded preview regions.

## Contract

- `register_apply_patch_tool(registry, *, file_state)` registers `apply_patch`
  in the `files` family with one required `patch` string. It is an ordinary
  Provider-neutral function Tool, not a Provider-native patch operation. The
  open model-facing schema's parameter list is enforced at dispatch: unknown
  arguments fail before the handler.
- The owner-selected argument repair (`normalize_patch_arguments`) accepts patch-text
  aliases (`input`, `patch_text`, `diff`, ...), ordinary field formatting and shared
  call wrappers. Equal aliases coalesce; conflicting aliases (including placeholder
  text) and unsupported fields fail before mutation. Patch contents remain literal.
  The advertised schema stays `patch` only.
- Other harnesses' shapes run when they name one exact change. Unadvertised root
  fields (`PATCH_HIDDEN_PARAMETERS`: `path`, `old_string`, `new_string`,
  `replace_all`, `expected_replacements`, `edits`, `content`, `insert_line`) are
  validated but never offered; spelling aliases match ignoring case, `_`, `-` and
  spaces (`file_path`, `filePath`, `old_str`, `file_text`, ...). Shapes: Claude Code
  Edit/MultiEdit/Write, Gemini `replace` (`expected_replacements`) and `write_file`,
  text-editor `command` `str_replace`/`create`/`insert` (`view` and `undo_edit` fail
  naming the next call), Hermes `mode` replace/patch, and `path` plus a headerless
  patch body. A single edit travels as an `edits` item and empty `content` as an
  empty Add File patch, because shared contract normalization drops empty
  unadvertised root values (`_carry_empty_text`). Remark fields (`explanation`,
  `instructions`, `description`, Roo's `line_count`; any spelling) are dropped, and
  so are switches while `false` (Windsurf `EmptyFile`, Roo `use_regex`/`ignore_case`,
  MCP filesystem `dryRun`). Turned on, each stays an unknown parameter, except
  `EmptyFile: true` without content, which creates an empty file. Windsurf
  `replace_file_content` chunks (`ReplacementChunks` of `TargetContent`/
  `ReplacementContent`/`AllowMultiple`) run as `edits`.
  Two kinds of change in one call, a `path` that
  contradicts the patch's file, incomplete old/new pairs and Cursor `code_edit`
  (placeholder comments leave the change open) fail before any effect.
  `old_string` replacements match precisely, else as a copy with errors
  (`copy_match`, below; never with `replace_all` or an expected count); an empty
  `old_string` creates a file or fills an empty one and fails with `file_exists`
  otherwise. `patch_targets(arguments)` lists every named path for callers that
  vet targets first (the provider probe).
- The description says one call can change several places in several files, in
  order, and that successful changes stay applied when another fails. The `patch`
  parameter carries one example (Update with an `@@` hint, Add, Delete, Move File)
  and the rules for `-`/`+`/space lines, `@@` text, insertion-only blocks and EOF
  appends, Add File replacement and path resolution. Other harnesses' fields are
  never described. Detailed continuation guidance belongs in results; matching
  errors distinguish missing/ambiguous context hints from hunk text.
- Add, Update, Delete, standalone `Move File: source -> destination`, and
  Update plus `Move to: destination` are supported. Paths use ordinary
  `ToolContext.resolve_path` semantics: cwd-relative or absolute, with resolved
  aliases sharing the same mutation history and lock. Add/Update change content
  and resolve through links to the target file. Delete/Move act on the named
  entry: a final symbolic link or Windows junction is deleted or renamed itself
  (`resolve_path(..., follow_final_link=False)`), never its target; a link at a
  move destination, even a dangling one, is an existing destination. Link moves
  and case-only renames (the same entry under another spelling on Windows) use
  one `os.rename` instead of copy-then-delete; relative link targets are not
  rewritten. They read as `Moved A to B.`; an Update through a link reports the
  link target's path (`test__patch_entries.py`).
- The complete patch structure is parsed before mutation; unparseable framing or
  operation syntax rejects the call without writes. Once parsed, each Update
  hunk and each Add/Delete/Move is attempted in order against actual current
  bytes. A failed hunk does not prevent later matching hunks, even in the same
  file. Add -> Update and Move -> Update observe completed earlier effects.
  Parent/file overlaps reject the affected entries, not unrelated files. Add
  creates or fully replaces files. Existing targets require a current Session read
  stamp; missing/stale stamps fail with `file_not_read`/`file_modified_since_read`.
  When the file is text of at most 16 KB and 400 lines and unchanged since the
  snapshot, that failure shows its whole numbered content and stamps it, so the
  same call succeeds when sent again; otherwise it names the `read` call.
  An identical Add is a verified no-op and does not require a prior read; an
  existing file that is empty or whitespace-only (after a BOM) needs no read.
  Empty Add bodies produce empty files; `\ No newline at end of file` suppresses
  the trailing newline. Replacement preserves existing line endings, UTF-8 BOM,
  and permission bits. Move destinations still reject different existing files.
- Failed creates/moves and uncertain writes block later entries touching those
  paths for this call ("was not tried because an earlier change ... did not
  complete"). A failed hunk in Update plus Move leaves successful hunks applied at
  the source and skips that operation's move with its own message (the file keeps
  its name). Other files continue.
- Results are plain text: success data is `{status, content}` (result schema
  requires both), `status` one of `applied`, `partial`, `unchanged`. Paths show
  relative to the call's cwd when inside it (`display_search_path`). Each file's
  net effect reads once: `Created X (N lines).`, `Replaced the content of X (...)`,
  `Deleted X.`, `Moved A to B.`, or `Updated X:` / `Updated A and moved it to B:`
  followed by the changed regions as they are now with `read`-style gutters
  (touching regions merge; more than two keep first and last plus an omission
  line; long lines show a window with an `N:C|` gutter). Notes (recoveries,
  metadata drift) and syntax warnings follow as `Note:` / `Warning:` lines. Syntax
  warnings compare the initial and final Tool-written text; a created or fully
  replaced file reports any final syntax error, even when the original was invalid.
- Mixed outcomes return `status: partial`, led by counts (`N of M changes
  applied; K did not.`, or `fully applied; K only in part` when a write was
  incomplete) and the instruction to resend only unfinished changes; each failure
  follows as `Failed:`/`Skipped:`/`Incomplete:` with its `where` (path plus
  `hunk N`/`edit N` when several). All-failed calls use the ordinary failure
  envelope (`all_changes_failed` for several) with the same failure text and
  `No file was changed.`; no applied effect hides behind a failure envelope.
- `unchanged` covers verified no-ops (`already has this content`, `already
  contains this change`, `already has that name`, identical old/new text) and
  changes that cancel out (`cancel each other out`). It never substitutes for
  failed or uncertain operations. WebUI reads only `status === 'partial'`.

## Matching and recovery

- Canonical framing is `*** Begin Patch` / `*** End Patch`; one enclosing
  Markdown fence (any info string), repeated Begin markers, a Begin marker between
  operations (it cannot be `+` content, so it only starts a frame), omitted
  framing, and successive frames are tolerated. Every frame is parsed before
  mutation. Body line prefixes retain their meaning even when content spells a
  patch marker. Headers match case-insensitively with synonyms (Add/Create/New,
  Update/Edit/Modify, Delete/Remove, Move/Rename); quoted or backticked paths are
  unquoted. A header repeated just before Begin Patch, and identical adjacent
  Update headers before a body, coalesce; a different empty Update target is not
  discarded (`empty_update`).
  Missing context prefixes and omitted `@@` are accepted; unknown operation
  headers, unframed prose, and body text after End Patch without a new file header
  are rejected. Explicit `@@` hunks following Move File use Update-plus-Move semantics.
- Format selection (`_parse`): any V4A header selects V4A; otherwise a `---`/`+++`
  pair or `diff --git` before the first `@@` selects unified diff (git `a/`/`b/`
  prefixes, `/dev/null` add/delete, `rename from`/`rename to` as Move, binary
  diffs rejected, hunk counts only gate `---`/`+++` detection); otherwise
  SEARCH/REPLACE blocks (Aider filename line, Cline `------- SEARCH`/`+++++++
  REPLACE`, Roo `:start_line:`; an empty SEARCH creates a file only if absent or
  empty); otherwise a headerless body updates the `path` field's file.
- Add bodies tolerate missing `+` prefixes, including blank lines, preserving the
  entire unprefixed line and its indentation. A bare opening `@@` is tolerated.
  Unprefixed removals, context hints and later hunk delimiters remain invalid;
  explicit `+` content remains literal, including leading patch syntax.
- `Move to` is operation metadata and may precede, separate, or follow Update
  hunks. Repeated identical destinations are harmless, including after Move File;
  conflicting destinations fail before any writes. Prefixed content remains literal.
- `@@ context` hints select successive unique whole lines at the hunk's section.
  The final hint may also appear as the first context/removal line. Multiple
  hints can narrow a section. Numeric unified-diff headers are advisory;
  content remains authoritative. `*** End of File` restricts matching to EOF;
  when a hunk with context or removed lines fails there, it is retried without
  the marker and, if that places it, applied with a warning naming the marker.
  Addition-only hunks insert after a hint or append without a hint; exact
  adjacent content at a hint makes repeated nonblank insertions no-ops.
  Unanchored appends always append because an existing suffix cannot distinguish
  a retry from an intentional repeated line.
- Match errors show bounded candidate excerpts with `read`-style gutters
  (`The closest text in the file, lines A-B:`); touching or overlapping excerpts
  merge. Missing targets use similarity-ranked diagnostics plus `First difference,
  line N: the file has '...' where the patch has '...'`, and note when the new
  text already occurs (a change made earlier; not when `old_string` contains
  `new_string`, as after a deletion); without candidates the text names
  the `read` call. Ambiguity reports the winning match's actual locations
  (including section offsets) under `Where it occurs:`, not guessed alternatives.
  An `expected_replacements` mismatch fails with `occurrence_mismatch`. These
  excerpts never authorize a write.
- After any text-entry failure on a path, remaining hunks and `old_string` edits
  on that path allow only unique precise matches (no `copy_match`), preventing
  tolerance from silently satisfying a failed earlier precondition. Other files
  retain normal tolerance.
- `fuzzy_match.replace_fuzzy` owns precise matching: exact, normalized (newline,
  Unicode, typography), line-trimmed, and whitespace-normalized, in that order.
  Patch-only options require whole-line matches and constrain EOF. The matcher
  retains its default substring mode for direct callers. Precise matches win;
  ambiguity at a winning strategy never falls through to a looser strategy.
  Ambiguity counts every occurrence, including overlapping ones (repeated
  closing-brace lines) and ones following a filtered partial occurrence, for
  hunks and `@@` anchors alike; `replace_all` still replaces leftmost
  non-overlapping spans.
- Newline/Unicode/typography/whitespace/indentation differences are supported
  for every hunk line. Old text copied with other errors is applied whenever the
  passage is clearly identified, with stricter evidence for shorter text (user
  decision 2026-09-25). `core/tools/copy_match.py` owns this for `apply_patch`
  hunks and `old_string` edits, `skill_manage` patches and Swarm Wiki edits; it
  runs only after every precise attempt and the already-applied check missed, and
  not when the new text is already present. Where (location), from the number W
  of correctly copied words: below 3 the copy is exact up to spacing; from 3 it
  may misspell one word per 3 correct words, and a kept (context) line may lack
  or add words or signs, one per 2 correct words; from 12 it may also differ
  otherwise, one difference per 4 correct words, but never where either side of
  a difference is an identifier (underscore, digit, or inner capital: another
  function or variable). Exactly copied first and last lines (3+ lines, 2+ words
  each) also place a passage around similar lines (SequenceMatcher ratio >= 0.5,
  or 0.7 when the anchor pair repeats). Each line keeps its place: a kept line
  must be mostly right (other differences <= correct words), and extra words
  that continue the line above or below mark a copy joined across a line break
  (a left-out blank line or wrapped line); such a passage refuses the edit
  instead of taking it one line off (Session replay 2026-09-26: a block landed
  after its heading, a new list item was indented). A misspelling is a word of
  4+ characters within one edit (two from 8 characters, case-insensitive,
  adjacent swaps count once) of the file's word, with the same digits, that
  occurs nowhere in the file. Passages copied up to misspellings and kept-line
  gaps win over looser ones; overlapping candidates are one passage, placed by
  the fewest differences (a tie between different spans stays ambiguous); more
  than one passage at the winning level is `ambiguous_match` (`ambiguous_copy`
  wording for `old_string`). What (merge): the change from old to new text is
  applied to the file's text. A line the edit writes comes out as the caller's
  new text up to the file's spelling of misspelled words: kept text in it must
  match the file up to misspellings and spacing, since a difference there may
  be wording the caller meant to write (replay: an intended change inside the
  old text was dropped). Lines the edit keeps stay as the file has them. The 3
  words or signs on each side of each change must match up to misspellings, and
  within a change other differences need 4 correct words each (a rewrite may
  differ; `3` to `4` where the file says `5` may not); changes separated only by
  spacing count as one. When the winning passage cannot take the change,
  nothing is applied elsewhere and the hunk fails `text_not_found`. Warnings
  name each replaced line and each kept line that differed (`... was edited
  anyway; it read:` / `... was left as it reads:`, long lines from just before
  the first difference) and each respelled word (`copy_warnings`). Tests:
  `test_copy_match.py`, plus the
  patch, field, Skill and Wiki suites.
- Only changed lines are emitted from the replacement; context lines keep their
  actual original bytes.
- Changed lines matched with different indentation are written in the file's
  indentation style: each indent the matched lines show maps to its file
  indent, and other indents (new deeper lines) convert level by level between
  the model's and the file's unit (tabs or N spaces, learned from the whole
  file), including a dropped outer level. Mixed tab/space output from a
  spaces-for-tabs model is a defect.
- Read-output gutters recover after raw matching misses, including single lines,
  mixed raw/numbered locators, and stale line numbers. Their stripped contents
  must identify a unique whole-line target; line numbers never resolve ambiguity.
  Standalone added blocks require complete consecutive gutters. Continuation
  gutters are rejected. Literal matching against gutter-shaped existing content
  takes precedence over correction.
- Patch-only typography normalization also recognizes expanded em dashes and
  ellipses, minus signs, and Unicode spaces. Precisely equivalent changed lines
  preserve original glyphs only in unchanged portions; explicit glyph changes
  and merely similar preimages do not trigger restoration.
- Escape normalization needs a failed literal match plus a precise match of
  the unescaped old text. Replacement decoding is limited to escape kinds
  evidenced in the locator; newline escapes remain literal and tab/carriage-return
  recovery is limited to indentation. Approximate matches cannot introduce
  doubled quote/backslash escapes absent from the actual target.
  Surplus blank boundary context can be dropped after the full locator misses.
  Blank lines explicitly marked for deletion remain meaningful operations.
- Context-only blocks before another `@@` become ordered precise locator hints
  for that next hunk, including multiline context. Missing or ambiguous anchors
  fail without falling back to a different location; duplicate matches after
  the anchor remain ambiguous. Anchors do not leak into subsequent edits or files.
  An entirely context-only patch fails with `no_changes`, explains that
  space-prefixed lines are unchanged lines, and says which file lines those lines
  match when they are found precisely (`The unchanged lines match X line(s) N-M.`).
  It never invents omitted replacement content or reports success. Identical old/new line sequences are no-ops only when located. A unique
  precise post-state with at least four shared non-whitespace context characters
  permits an already-applied retry before approximate matching. A single-line
  replacement can also identify its post-state through the unchanged prefix and
  suffix within that line: both must retain substantive text, and together they
  must select exactly one current line. Exact post-state text without an independent
  target is not enough; approximate matching cannot substitute that text or another
  similar target. Explicit hints and EOF constraints still apply. A missing
  deletion/move source remains an error, not inferred proof of prior execution.

## Mutation invariants

- One Runtime's shared FileReadState locks cover every resolved path, acquired
  in deterministic order and held across the call. Each entry is planned on its
  own; existing-target Updates need no prior read. Bytes and mode are checked
  after planning, before mutation, and after completed writes. Later entries
  detect drift from earlier observations and leave affected paths alone.
- Updates reject NUL bytes and invalid UTF-8. BOM, surviving context bytes,
  existing EOF newline state, and file permissions are preserved. Lines are
  delimited like `read` (LF, CRLF, lone CR only; U+2028 and similar separators
  are line content). New lines adopt the detected CRLF/LF/CR style, else LF;
  explicit no-newline markers apply only at EOF.
  Binary files may be moved or deleted without text decoding.
- Writes reuse `atomic_write_bytes`; its optional `mode` carries source
  permissions to a move destination. Its `before_replace` callback rechecks all
  current operation paths before each bounded retry of Windows replacement
  errors 5/32/33; completed entries are never replayed. Exhausted failures report
  retry eligibility and the actual attempt count. A destination is written and checked before
  its source is deleted; both paths are rechecked before deleting the source.
  A move that wrote its destination but could not delete its source reports
  completed/pending paths and blocks follow-up entries on both. An observation
  failure after writing also retains the completed effect in a partial result.
  No rollback is claimed. Locks cannot exclude unrelated external writers;
  checks plus atomic replace are not a portable filesystem compare-and-swap.
- Successful surviving files, including verified no-ops, receive Session read
  stamps. Metadata drift can produce a post-success warning. Text mutations
  feed the existing ChangeTracker with actual before/after contents and publish
  presentation-only line/file counts. Updates reuse syntax-delta warnings; Add uses full-file syntax warnings.
- The handler uses the shared cancellation-shielded Tool worker boundary so
  an in-flight mutation settles before cancellation returns.

## Verification

- `tests/core/tools/test_apply_patch.py` covers framing and matching;
  `test_apply_patch_operations.py` covers byte preservation, ordered plans,
  read stamps, statistics, syntax warnings, and display metadata;
  `test_apply_patch_transactions.py` covers partial effects, failure containment,
  locking, cancellation, and guarded retries;
  `test_apply_patch_overwrite.py` covers creation/replacement, read guards,
  empty contents, format preservation, and concurrent drift.
- `test_apply_patch_recovery.py` covers Add syntax repair, context-only failure
  and insertion, shared locks across Sessions, bounded replacement retry and
  source/destination drift. `test_file_state.py` includes a real Windows reader
  handle without delete sharing, not just injected exceptions.
- `test_apply_patch_session_regressions.py` covers Session-derived concatenated
  frames, repeated headers, argument aliases/conflicts and preserved context anchors.
  `test_apply_patch_fields.py` runs other harnesses' shapes (Edit, MultiEdit, Write,
  text-editor commands, Hermes mode, SEARCH/REPLACE, unified/git diffs) through
  production dispatch, including empty-text edits, the read guard and conflicts.
  `scripts/tool_lab/cases/files.json` holds the Model-visible result cases.
- Existing fuzzy-match, file-state, Runtime and Provider-schema
  tests cover the shared boundaries.
  `tests/core/providers/test_ollama.py` verifies intact patch arguments through
  Cloud response normalization and Chat ingestion.
- `python -m scripts.probe_provider_tool_call --scenario apply_patch` uses the
  production registry and disposable files. Its matrix separates natural batching tasks
  (no imposed call count or prebuilt arguments), exact edge/invalid requests,
  and continuation from an actual partial Tool Result with candidate excerpts.
  It vets targets through `patch_targets`, rejects imports from a different
  checkout, checks file effects, result `status` and expected result text
  (`mentions`), and verifies that recovery avoids replaying a completed append. Natural
  tasks also cover insertion, EOF targeting, and recovery from missing/ambiguous
  hints. Exact cases pair move-metadata recovery with conflicting destinations and
  literal move-shaped text. Probe tests reject scope escapes and false completion.
  The probe supplies Adapter-owned request context for gateway routing. Its matrix
  includes missing Add prefixes, conflicting syntax and context-only recovery.
