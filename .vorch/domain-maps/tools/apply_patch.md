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
  call wrappers. Equal aliases coalesce; a placeholder alias (`""`, `null`, `...`)
  beside a real patch is ignored (`placeholder_as_omitted`). An object, or JSON
  text of one, under a patch spelling whose keys are all call fields wraps them
  (`wrapping_fields`): `input: {"path": ...}` beside `patch` supplies `path`, which
  must agree with the patch's file headers (Sessions, 2026-09). Conflicting aliases
  fail before mutation, naming both keys and values (`Conflicting values for patch:
  patch is "..." and input is "...". Send only the intended one.`; shared
  `normalize_call_arguments` wording). Unsupported fields fail as well. Patch
  contents remain literal. Normalizer refusals end with `No file was changed.`
  (`_normalize_call`). The advertised schema stays `patch` only.
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
  (placeholder comments leave the change open; the error shows a patch skeleton
  with the call's real path) fail before any effect. An `old_string` alone beside
  a nonempty patch (no `new_string` or `insert_line`) is a copy of the lines the
  patch changes: the patch runs and the result adds `old_string was ignored
  because patch describes the change.` (`patch_ignores_old_string`). Evidence: 3
  such calls in one Swarm run, all `old_text` beside a complete patch, were
  refused as two kinds of change (Sessions, 2026-09).
  `old_string` replacements match precisely, else as a copy with errors
  (`copy_match`, below; never with `replace_all` or an expected count); an empty
  `old_string` creates a file or fills an empty one and fails with `file_exists`
  otherwise. `patch_targets(arguments)` lists every named path for callers that
  vet targets first (the provider probe).
- The description says one call can change several places in several files, in
  order, and that successful changes stay applied when another fails. The `patch`
  parameter carries one example (Update with an `@@` hint, Add, Delete, Move File)
  and the rules for `-`/`+`/space lines (each a whole line), `@@` text,
  insertion-only blocks and EOF appends, Add File replacement and path
  resolution; `## Agent-facing text` records why each sentence is there.
  `test_the_example_in_the_patch_description_applies` runs the example. Other
  harnesses' fields are never described. Detailed continuation guidance belongs in results; matching
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
  link target's path (`test_apply_patch_files.py`).
- The complete patch structure is parsed before mutation; unparseable framing or
  operation syntax rejects the call without writes. Once parsed, each Update
  hunk and each Add/Delete/Move is attempted in order against actual current
  bytes. A failed hunk does not prevent later matching hunks, even in the same
  file. Add -> Update and Move -> Update observe completed earlier effects.
  Parent/file overlaps reject the affected entries, not unrelated files. Add
  creates or fully replaces files. Existing targets require a current Session read
  stamp; missing/stale stamps fail with `file_not_read`/`file_modified_since_read`.
  When the file is text of at most 16 KB and 400 lines and unchanged since the
  snapshot, that failure shows its whole numbered content (CRLF/CR shown as plain
  line breaks) and stamps it, so the same call succeeds when sent again;
  otherwise it names the `read` call.
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
  entire unprefixed line and its indentation. Lines starting with `-` are content
  too while the Add creates a file or fills an empty one. Where it would replace
  content, the first such line fails that operation in `_plan` (`add_minus_line`),
  because it can be a removal meant for Update File. Evidence: all 4 Add refusals
  on `-` lines in Sessions up to 2026-09 were content of new files (underlines,
  SQL comments, list items), and the retries rewrote or dropped those lines. A
  bare opening `@@` is tolerated; context hints and later hunk delimiters remain
  invalid (`invalid_add_line`). Explicit `+` content remains literal, including
  leading patch syntax.
- `Move to` is operation metadata and may precede, separate, or follow Update
  hunks. Repeated identical destinations are harmless, including after Move File;
  conflicting destinations fail before any writes. Prefixed content remains literal.
- `@@ context` hints select successive whole lines at the hunk's section.
  The final hint may also appear as the first context/removal line. Multiple
  hints can narrow a section. A hint that occurs several times is read from its
  first occurrence, as in Codex, but only while the hunk's lines then match once
  through a precise strategy (not `copy_match`, not an already-applied
  post-state, not an addition-only hunk); otherwise it fails with
  `ambiguous_context`. That one place is right whichever occurrence was meant.
  Evidence: in all 4 `ambiguous_context` failures of one Swarm, the hunk's lines
  occurred once in the file (Sessions, 2026-09). Lines a precise strategy finds several times are
  ordered as in Codex: after a hint, the first occurrence from the hint on is
  changed; a hunk without hints takes the first occurrence from the line where
  the same Update's last completed change ended (`_apply_hunks`,
  `_Batch.change_ends`, `replace_fuzzy(first=True)`), and fails with
  `ambiguous_match` when none follows. A note names the count and the changed
  line. Without a hint or an earlier change of that Update, and for `copy_match`
  passages, several occurrences stay `ambiguous_match`, unless as many hunks of
  that Update without a hint name the same lines as the file holds them
  (`_Hunk.twins`, counted in `_parse`): then the first is changed and the later
  ones follow as above. As in Codex, where each hunk searches after the previous
  one, only that order lets every hunk find its occurrence. Evidence (Sessions since
  2026-09-01, 89 ambiguous hunks): the Agent's later successful edit targeted the
  first occurrence after the hint in 16 of 16 and after the previous hunk in 44
  of 45; bare first hunks meant the file's first occurrence only 11 of 15 times,
  too few to choose silently. In one Swarm run, both refused bare first hunks
  with as many identical hunks as occurrences meant them in order (Sessions,
  2026-09). Numeric unified-diff headers are advisory;
  content remains authoritative. `*** End of File` restricts matching to EOF;
  when a hunk with context or removed lines fails there, it is retried without
  the marker and, if that places it, applied with a warning naming the marker.
  Addition-only hunks insert after a hint or append without a hint; exact
  adjacent content at a hint makes repeated nonblank insertions no-ops.
  Unanchored appends always append because an existing suffix cannot distinguish
  a retry from an intentional repeated line. When the first nonblank `+` line
  reads like the last hint line rewritten (a hint line of at least 20
  characters, 80% of them recurring in order; `_rewrites_hint`), the insertion
  adds `Note: The + lines were inserted below the @@ line '...', which stays in
  the file, and the first of them resembles it. If that line was meant to be
  replaced, remove it with a - line.` Evidence: one Session inserted rewritten
  table rows below the rows it named after `@@` several times, and the stale
  rows piled up; 7 failed patches followed (Sessions, 2026-09).
- When the `@@` lines do not place a hunk (`context_not_found`, or
  `text_not_found` after them), `_without_hints` retries it without them: if
  its unchanged and removed lines then match exactly one place through a
  precise strategy (`precise_only`: no `copy_match`, no ordering by an earlier
  change), it is applied there with a note naming the line: `The @@ line '...'
  was not found, but the lines to replace occur once in the file; they were
  changed there, at line N.`, `The lines to replace are not after the @@ line
  '...', but ...`, or for a context-only block `The lines of the @@ block above
  the lines to replace were not found together, but ...`. Otherwise the original
  error stands; addition-only hunks never fall back. Evidence: in one Swarm run,
  11 failed hunks matched once exactly without their `@@` lines, which were
  paraphrased, cut from a longer line, a unified-diff range, or below the
  target. Each replay changed only the lines the hunk names; the Agent's later
  edits changed the same lines in 10, and it dropped the eleventh change.
  `copy_match` would have placed one 250 lines off (Sessions, 2026-09).
- Match errors show bounded candidate excerpts with `read`-style gutters
  (`The closest text in the file, lines A-B:`); touching or overlapping excerpts
  merge. Missing targets use similarity-ranked diagnostics plus `First difference,
  line N: the file has '...' where the patch has '...'` (`where old_string
  has` for `old_string` edits). Candidate windows come from the three longest
  copied lines and from distinctive copied lines (at most 3 occurrences) the file
  holds exactly; a window at least 2 such lines place is shown even below the
  0.60 similarity floor (`find_closest_candidates`). The line-by-line comparison
  (`fuzzy_match.first_difference`, shared with Swarm Wiki edits) starts at the
  first copied line with a word the file holds near the window, because a window starts wherever its best-matching lines
  put it, shifted by every line the copy added or dropped before them. Closing
  quotes and brackets occur too often to align by; when the file holds no other
  copied line there, the nearby file line most similar (>= 0.50) to the start of
  the first copied line starts it (a reworded copy resembles its line; a copy
  that joins lines starts like the first of them, which whole-line similarity
  missed once in a replay), else the window start does. Evidence:
  the longest lines were often added text copied without `+`, so 43 of 133 failed
  hunks in one Swarm said `No similar text` although most copied lines were in
  the file, and 10 of 76 checkable first differences named a line one to seven
  lines too early (Sessions, 2026-09). Long differing lines show <=240-character windows
  centered on the first substantive mismatch, with 1-based file/copied-line
  character coordinates and explicit truncation. Truncated candidate excerpts
  name a callable `read(path=..., offset="line:character", limit=...)` continuation;
  positions count Unicode characters and preserve LF/CRLF/CR line semantics.
  Overlapping excerpts merge before continuations are named: a continuation
  starts after the text any excerpt shows, and overlapping ones join into one
  call. Separately named continuations of adjacent candidates mostly repeated
  shown lines (26 of 34 merged reports, Sessions, 2026-09).
  Diagnostics also note when the new
  text already occurs (a change made earlier; not when `old_string` contains
  `new_string`, as after a deletion); without candidates the text names
  the `read` call. Ambiguity reports the winning match's actual locations
  (including section offsets) under `Where it occurs:`, not guessed alternatives.
  An `expected_replacements` mismatch fails with `occurrence_mismatch`. These
  excerpts never authorize a write.
- A patch line that occurs only inside longer file lines (a fragment copied as a
  line) is named before similarity candidates when it sits in at most 3 lines:
  `The patch line '...' is only part of line(s) N. Each patch line is a whole
  line, so copy all of line N:` plus those lines (`part_of`, `_part_of_lines`).
  A file line counts only when the text reads as cut from it (`_cut_from`): at
  least 3 letters or digits, no word of the line split, and at the line's start
  or end or at least 8 characters long. Evidence: of 18 such reports over three
  days, 4 named a line that held `}`, `pass` or `1,` by chance; the others met
  this rule (Sessions, 2026-09).
  Otherwise the closest-text candidates show. Without candidates, the report
  names the hunk's first unchanged or removed line that no file line matches
  (ignoring spacing, `absent`): `The patch line '...' is not in the file.` plus,
  for a `-` line, that it must match a line of the file; for an unchanged line
  between or right next to `+` lines (`beside_additions`), that without `+` it
  must already be in the file and a new line starts with `+`; for another
  unchanged line, `A line without + or - is unchanged, so it must match a line
  of the file.` `No similar text` remains only when every such line occurs somewhere.
  Evidence: in one Swarm run, 6 failures said only `No similar text`; each held
  a line the file lacked (new lines after the last `+` line written without `+`,
  a misremembered or output-copied context line), and 4 were followed by a
  `read` before the retry, 1 by the same mistake again (Sessions, 2026-09).
  When that line is an unchanged line beside `+` lines, its report replaces the
  `part_of` report: a new line written without `+` can also occur inside a
  longer file line, and calling it a fragment hides the missing `+`.
  `old_string` text is compared by its first difference instead.
- A hunk that is exactly one `-` line and one `+` line, whose `-` text is not a
  whole line but occurs exactly once inside one line of the matched window
  (overlaps count), is replaced within that line, with `Note: The - line is part
  of line N; only that part of the line was replaced.` (`_replace_within_line`,
  after the post-state check and before `copy_match`). Context lines, deletions
  without `+`, EOF markers or several occurrences keep whole-line semantics and
  fail.
- A match failure (`text_not_found`, `context_not_found`, `ambiguous_match`,
  `ambiguous_context`) on a file changed after this Session's last read appends
  `X changed after this Session last read it; read it again before resending.`
  (`_STALE_FAILURE`); the patch was likely written against old content.
- After any text-entry failure on a path, remaining hunks and `old_string` edits
  on that path allow only unique precise matches (no `copy_match`), preventing
  tolerance from silently satisfying a failed earlier precondition. The failure
  also drops that path's `change_ends`, since the failed target still holds its
  old lines; only a hint then orders occurrences. Other files retain normal
  tolerance.
- `fuzzy_match.replace_fuzzy` owns precise matching: exact, normalized (newline,
  Unicode, typography), line-trimmed, and whitespace-normalized, in that order.
  Patch-only options require whole-line matches and constrain EOF. The matcher
  retains its default substring mode for direct callers. Precise matches win;
  ambiguity at a winning strategy never falls through to a looser strategy
  (`first=True` takes that strategy's first match instead).
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
  a difference names another target: lexical identifiers (underscore, digit, or
  inner capital), plus names evidenced by calls, declarations and member/namespace
  access even when they are plain lowercase words. This evidence covers omitted
  names too, so substring recovery cannot drop a row ID to find another row.
  Misspellings still qualify under the rules below. Exactly copied first and last lines (3+ lines, 2+ words
  each) also place a passage around similar lines (SequenceMatcher ratio >= 0.5,
  or 0.7 when the anchor pair repeats), but cannot override a copied name that
  exists elsewhere in the file. Plain prose does not acquire code-name meaning
  merely by mentioning a function or class. Each line keeps its place: a kept line
  must hold at least half of its file line's words, and half of its words and
  signs together (a blank file line holds none; counting words keeps shared
  markup such as `##` from making another heading look right), and extra words
  that continue the line above or below mark a copy joined across a line break
  (a left-out blank line or wrapped line); such a passage refuses the edit
  instead of taking it one line off (Session replay 2026-09-26: a block landed
  after its heading, a new list item was indented). For a kept line only a
  neighbor outside the passage counts: words moved across a break inside the
  passage (a rewrapped comment) leave the kept line as the file has it; a
  written line still counts neighbors inside, since its new text could drop or
  repeat the moved words. A misspelling is a word of
  4+ characters within one edit (two from 8 characters, case-insensitive,
  adjacent swaps count once) of the file's word, with the same digits, that
  occurs nowhere in the file. Some differences never matter: a hyphen (or two)
  for an em dash, a lone backslash only one side holds, and zero-width
  characters (U+200B-U+200D, U+2060, U+FEFF). They are neither correct words
  nor differences, and kept text keeps the file's form, including the file's
  zero-width characters beside it; zero-width characters the caller writes at
  the edge of a change, other than its copy's there, refuse. A backslash
  difference means the copy escapes differently: the text the edit writes may
  then hold neither a backslash nor a sign the file escapes where the copy does
  not, on any line of the passage (a copy that drops the file's escapes comes
  with new text that drops them too, so a new line would lack them); a
  backslash only the file holds must not border a change (it would escape the
  new text). A read-gutter leftover (`||` for
  `|`) is not such a difference: new text would repeat it. Passages copied up
  to misspellings and kept-line gaps win over looser ones; overlapping
  candidates are one passage, placed by the fewest differences (a tie between
  different spans stays ambiguous); more
  than one passage at the winning level is `ambiguous_match` (`ambiguous_copy`
  wording for `old_string`, `ambiguous_patch_copy` for hunks, which asks for an
  exact copy instead of an `@@` line). What (merge): the change from old to new text is
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
  spaces-for-tabs model is a defect. When most indented matched lines already
  have the file's indentation, the replacement is written as sent: the other
  lines are typos, and mapping them re-indented new lines wrongly (Session: a
  `-` line one level too deep put the new block one level too shallow). One
  space is never a level unit; when no wider unit explains the indents, new
  lines keep their offset instead of every space becoming a file level.
- Read-output gutters recover after raw matching misses, including single lines,
  mixed raw/numbered locators, and stale line numbers. Their stripped contents
  must identify a unique whole-line target; line numbers never resolve ambiguity.
  A run of added lines is recovered only when every line carries a gutter;
  complete but nonconsecutive or continuation gutters are rejected
  (`line_numbered_content`). Only unchanged and removed lines can make a miss a
  gutter error. Two or more gutter-shaped lines among other added lines are
  content, such as `N|value` data rows, and are written as sent with a note
  quoting one of them. Literal matching against gutter-shaped existing content
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
  A blank last context line matches the empty line past the file's final line
  break; `+` lines after it end with that line break, so the file keeps it.
- Unprefixed or space-prefixed V4A Update lines between two `+` lines parse as
  context (`_Hunk.written` keeps each line as written). When the hunk, its other
  recoveries and `copy_match` all miss, `_unmarked_readings` re-reads such runs as
  `+` lines: first only runs holding a nonblank line the file lacks, then also
  blank runs, then all runs, so a run the file has stays context while that
  places the hunk. A blank run stays context only between context/removal lines
  that stay (`_with_edge_blanks`): beyond them, the file's own blank line there
  would place it and move into the added block, so the blank line after the
  block would go missing. `_unmarked_texts` adds each line as written
  (whitespace-only becomes blank). A Model that wrote the space prefix of
  unchanged lines instead of `+` indents such a line one space too deep, so
  `_meant_line` decides line by line: a line keeps its indentation when the
  nearest new-text line above or below has it, or when the line above aligns
  continuations to that column (just after a bracket it leaves open, or where
  the last item inside it starts; `_alignment_columns`). Otherwise it loses its
  first space when the indentation then fits those lines, or when that space
  makes the indent width odd both from the line start and beyond the line
  above's indent. Evidence: in one Swarm run, 2 of 55 re-reads applied
  wrongly, one adding a real context line `]` a second time after the block
  because two blank runs kept it from placing the hunk without it, one
  indenting two statements one space too deep. All 11 of that run's 128
  re-readable runs that the indentation rule strips were written with the space
  prefix of unchanged lines (Sessions, 2026-09). In the next Swarm run, some
  runs mixed lines with and without that prefix, which a rule for the whole run
  left one space off, and 4 re-reads moved a blank line of the file into the
  added block. Over that run's 90 re-readable calls, the line rules changed only
  lines that were off; a plain odd-width rule without the alignment columns and
  the relative check misaligned hanging and aligned continuation lines
  (Sessions, 2026-09). A reading needs at
  least one remaining context/removal line and applies only through precise
  matching (`precise_only`: no `copy_match`), so the file must hold the
  surrounding lines adjacent; a failing reading falls back to the original error. A re-read run
  with no context/removal line after it (or before it) is placed on one side
  only, so the reading is dropped when the file continues on that side with one
  of the run's lines or a near copy (similarity >= 0.80, `_repeats_neighbors`):
  the run was context that differs from the file, and adding it would repeat
  those lines one space deeper. Evidence: in a replay of one Swarm's last 18
  minutes, 2 of 5 such hunks applied with duplicated lines before this check
  (Sessions, 2026-09). Success adds a
  note naming the lines, saying `Nothing more is needed for those lines` (or
  `that line`), and asking for `+` on every added line in later patches.
  Evidence: after the note without that sentence, the next call read the same
  file again in 28 of 94 cases, against 5% after other successes (Sessions,
  2026-09). Runs before a block's first `+` line are never re-read: a typo in
  leading context would otherwise duplicate the line. Runs right after a
  block's last `+` line (`_trailing_runs`) are re-read only after every reading
  of the runs between `+` lines missed: first up to the run's last line the file
  lacks, the rest staying context, then the whole run. Such a run ends before a
  blank line followed by a line indented at least 2 columns less than the
  block's last line (`_block_end`): that line opens the next section, which the
  Agent copied as context. Evidence: one Model family often left the `+` off
  statement continuation lines and new last lines. Replaying two Swarm runs,
  re-reading those runs applied 26 and 16 failed hunks, each as the hunk
  describes; the one wrong reading added a section heading comment after a blank
  line, which `_block_end` now keeps as context (Sessions, 2026-09). A reading
  that matches several places fails with that `ambiguous_match` instead of the
  original error, whose report would show no first difference. Success notes
  name the lines as `next to + lines`. When no reading applies and the
  reported first difference is such a line or an unchanged line right next to a
  `+` line, the report adds `That patch line has no + prefix, so it must already
  be in the file there; if it is new, start it with +.` (difference key
  `unprefixed`), since identical retries followed the bare difference. Session
  shape for leading runs: a new line before the `+` lines written without `+`.
- Context-only blocks before another `@@` become ordered precise locator hints
  for that next hunk, including multiline context. A missing anchor falls back
  only as the `_without_hints` rule above allows, and repeated anchors follow the
  hint rule above. A multiline block that is not found fails `context_not_found`
  with the `context_block_not_found` wording (`the lines of the @@ block above
  the lines to replace were not found together`), the closest text and its first
  difference. The former report named the block's first line as not found,
  although the file had it (1 failure, Sessions, 2026-09). Duplicate matches after
  the anchor resolve to the first (see the `@@ context` rule above). Anchors do
  not leak into subsequent edits or files.
  An entirely context-only patch fails with `no_changes`, says that without a
  `-` or `+` line every line stays unchanged, and repeats the description's
  replace/insert-above rule. For each context-only block (at most 3) whose lines
  are found, it shows where: `The unchanged lines match X line(s) N-M:` with the
  matched lines and 2 lines around them numbered like `read`, or, for several
  occurrences, `occur K times` with the first 3 excerpts and asks for more
  unchanged lines.
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
- Updates reject NUL bytes and invalid UTF-8. New text holding a NUL character
  (an Add, or an Update's result) fails `binary_file` with the `nul_text`
  wording: only binary files hold it, and source code writes its escape
  sequence, such as `\x00`. The binary-file wording offered Delete File and Move
  File for a file an Add was creating (Sessions, 2026-09). BOM, surviving context bytes,
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
  Filesystem failures name their reason in English without the absolute path
  (`file_state.os_error_reason`, e.g. `another program is using it`), not the
  localized Windows message. A write, delete or rename that another program
  blocks (Windows 32/33) says the file is unchanged and to send the change again
  after that program finishes, instead of asking to check the path; exhausted
  retries add `The write was attempted N times.` Swarm Agents met a file held
  open for 5 to 30 seconds and read the former `permission denied` as a real
  permission problem (Sessions, 2026-09).
- Successful surviving files, including verified no-ops, receive Session read
  stamps. Metadata drift can produce a post-success warning. Text mutations
  feed the existing ChangeTracker with actual before/after contents and publish
  presentation-only line/file counts. Updates reuse syntax-delta warnings; Add uses full-file syntax warnings.
- The handler uses the shared cancellation-shielded Tool worker boundary so
  an in-flight mutation settles before cancellation returns.

## Agent-facing text

| Text | Reason |
|---|---|
| `Edit, create, delete or move files with a patch.` | Names all four effects, so deletes and renames do not go through the shell, which bypasses read stamps and change tracking (F1). |
| `One call can change several places in several files; the changes apply in order, and changes that succeed stay applied if another one fails.` | Invites batching instead of one call per place (F6), and states the partial semantics, so an Agent resends only failed changes instead of replaying applied ones (F3, F5). |
| `patch`: the example (Update with `@@` hint, Add, Delete, Move File) | The V4A format is not universal; one example shows every header form once (F2). `test_the_example_in_the_patch_description_applies` keeps it valid. |
| `patch`: `Under Update File, the lines of an @@ block follow the file from top to bottom: lines starting with a space stay unchanged, - lines are removed, and + lines are added at their position.` | Defines the three prefixes and that a line's position in the block is its place in the file (F2, F3). |
| `patch`: `Each is a whole line; copy - and unchanged lines exactly from the file.` | Models sent a fragment of a long line as a `-` line (Sessions, 2026-09) (F2). Exact copies avoid relying on `copy_match`. |
| `patch`: `To replace a line, write it as a - line; to insert above a line, write the + lines before it.` | Context-only patches (`no_changes`, about 3% of one Model's patches in Sessions since 2026-09-10) mostly end after one or two unchanged lines that the follow-up patch replaced, inserted above or rewrote: the Model opened the block with the target line as unchanged, which rules out those edits (F2). |
| `patch`: `Every @@ block needs a - or + line.` | A block without changes fails with `no_changes` (F2). |
| `patch`: `Text after @@ is optional and names an earlier line, such as the enclosing function.` | Optional, so an Agent does not invent a hint; the example shows what a hint names. |
| `patch`: `Start another @@ block for another place in the same file.` | Avoids repeated Update headers and long context spanning distant places (F6). |
| `patch`: `A block of only + lines goes after the @@ line, or at the end of the file after a bare @@.` | Without it, where pure insertions land is a guess (F3). |
| `patch`: `Add File creates a file or replaces all of its content.` | Add File replacement is a vBot extension that replaces the former write Tool; without it, Agents delete and re-add or write through the shell (F1). |
| `patch`: `Paths are relative to the working directory or absolute.` | States both accepted forms; the System Prompt names the working directory. |

## Verification

- `tests/core/tools/test_apply_patch_calls.py` covers the description example,
  display metadata, patch spellings and wrappers, and other harnesses' shapes
  (Edit, MultiEdit, Write, text-editor commands, Hermes mode, SEARCH/REPLACE)
  through production dispatch, including empty-text edits and refused open or
  conflicting calls. `scripts/tool_lab/cases/files.json` holds the Model-visible
  result cases.
- `test_apply_patch_parsing.py` covers framing, concatenated frames, malformed
  patches, Add syntax repair, unified/git diffs and move headers.
- `test_apply_patch_matching.py` covers matching: gutters, escapes, copied text
  with misspellings, code targets, typography, context hints and anchors,
  within-line replacement and changes already in the file.
- `test_apply_patch_reports.py` covers previews, `no_changes`, long mismatch
  evidence and failure -> displayed read continuation -> successful correction,
  including Unicode and LF/CRLF/CR, plus `part_of` diagnostics.
- `test_apply_patch_files.py` covers byte and format preservation, read guards,
  paths, links, syntax warnings, read stamps and statistics, locking across
  Sessions, cancellation, failure containment, concurrent drift and bounded
  guarded retries. `test_file_state.py` covers which Windows sharing errors are
  retried behind a precondition check and includes a real Windows reader handle
  without delete sharing, released during the retries or held through them,
  not just injected exceptions.
- `test_copy_match.py` covers shared prose/identifier distinctions and target
  IDs in substring recovery.
- Existing fuzzy-match, file-state, Runtime and Provider-schema
  tests cover the shared boundaries.
  `tests/core/providers/test_ollama_cloud.py` verifies intact patch arguments through
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
