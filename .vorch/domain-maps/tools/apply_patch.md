# Apply Patch Tool

Applies ordered V4A file operations. It is the patch dialect of the file edit Tools
and the one the user configures: `edit` and `write` (`edit.md`, `write.md`) follow it
and are offered instead of it to Models outside the GPT families, one dialect per
prompt epoch (`edit.md` -> Dialect helpers). It also runs calls shaped for other
harnesses' edit/write Tools.
Add File creation-or-replacement is a vBot extension to the V4A-style interface.
`core/tools/apply_patch.py` owns the Tool's definition, request failures, display and registration. Its internal `_patch_requests.py` turns other harnesses' argument shapes into canonical fields and parsed operations; `_patch_syntax.py` owns V4A parsing and parsed operation values; `_file_changes.py` owns the change pipeline all three file edit Tools share (resolving and locking paths, planning steps in memory, the read guard, atomic commits, rechecks, read stamps, change statistics and file reports; this map's Contract and Mutation invariants describe it); `_edit_engine.py` owns locating and splicing one file's hunks and `old_string` replacements in current text (a fixed sequence of matching steps per kind of change); `_patch_entries.py` owns entry snapshots, Delete/Move entry resolution, and entry renames; `_patch_report.py` owns the Model-facing result text; `_change_preview.py` owns bounded preview regions.
A step is the pipeline's unit that is written at once: `apply_patch` runs each
hunk, Add, Delete and Move as its own step (partial success below), `edit` runs its
whole call as one step that writes the file once (`run_operations(...,
one_write=True)`; its text changes that fail are skipped and reported, the others
written, `tools/edit.md` -> Semantics), `write` one Add.
Shared failure messages are worded per Tool through `ChangeBatch.templates`
(`change_batch(context, templates)`) over `apply_patch`'s defaults (`_patch_syntax._MESSAGES`).

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
  contents remain literal. Normalizer refusals open with `No file was changed.`
  (`_normalize_call`). The advertised schema stays `patch` only.
- Other harnesses' edit shapes run when they name one exact change. Unadvertised
  root fields (`PATCH_HIDDEN_PARAMETERS`: `path`, `old_string`, `new_string`,
  `replace_all`, `edits`, `content`) are validated but never offered; spelling
  aliases match ignoring case, `_`, `-` and spaces (`file_path`, `filePath`,
  `old_str`, `oldText`, `file_text`, ...). Shapes: Claude Code Edit/MultiEdit/Write,
  Gemini `replace` and `write_file`, the MCP filesystem server's `edit_file`
  (`edits` of `oldText`/`newText`), and `path` plus a headerless patch body. A
  single edit travels as an `edits` item and empty `content` as an empty Add File
  patch, because shared contract normalization drops empty unadvertised root
  values (`_carry_empty_text`). Remark fields (`explanation`, `instructions`,
  `description`; any spelling) are dropped, and so is `dryRun` while `false`.
  Other harnesses' commands and switches fail at dispatch validation as unknown
  fields that change the call (`tools.md` -> Unknown fields; text-editor `command`,
  Hermes `mode`, `insert_line`, `expected_replacements`, Cursor `code_edit`,
  Windsurf chunks); the edit engine rebuild (2026-10) dropped them, and they return
  only with replay evidence. Other unknown fields run without effect, with a note. Two kinds of change in one call (also an `old_string`
  beside a nonempty patch), a `path` that contradicts the patch's file,
  incomplete old/new pairs and `replace_all` without `old_string` fail before any
  effect. `old_string` replacements match as substrings, precisely, else as a copy
  with errors (`copy_match`, below; never with `replace_all`); an empty
  `old_string` creates a file or fills an empty one and fails with `file_exists`
  otherwise. A replacement whose `new_string` is its `old_string` without the
  final line break is refused where the file continues after the match
  (`line_join`), and one that also changes text keeps the file's line break
  where the file continues after it and its last line holds text (`edit.md` ->
  Results). `patch_targets(arguments)` lists every named path for callers that
  vet targets first (the provider probe).
- The definition is minimal (user decision 2026-10-04): the description names
  its effects, and `patch` names its frame and that Add File replaces an existing file. The patch route is offered only to
  GPT families, which know V4A from Codex, so the format, the hunk rules and the
  partial semantics are taught by results and errors, not by definition text.
  `test_apply_patch_has_a_minimal_definition` pins it. Other harnesses' fields are
  never described. Matching errors distinguish missing/ambiguous context hints from
  hunk text.
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
  Each step resolves its paths when it runs, so a Delete or Move of a link
  earlier in the patch is observed: a later Add at the link's name creates a
  file there and never writes through the old link, and a later Update of a
  moved link's new name edits its target. A path that resolves outside the
  paths locked at the start (a link changed by someone else meanwhile) fails
  that step with `path_redirected`. A completed move whose source name gets a
  new file reads as `Moved A to B.` plus `Created A`.
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
- Results are plain text: success data is `{content}` alone (the result schema
  requires it); the outcome is in the text, not in a field, because the Model
  reads data fields as `name: value` lines and `status: applied` told it nothing
  the text does not (user decision 2026-10-10). Code that needs the outcome
  (replay, probes) derives `applied`, `partial` or `unchanged` with
  `_patch_report.edit_outcome`; results recorded before 2026-10-10 still carry
  `status`. Paths show relative to the call's cwd when inside it
  (`display_search_path`). Each file's net effect reads once: `Created X (N
  lines).`, `Replaced the content of X (...)`, `Deleted X.`, `Moved A to B.`, or
  `Updated X (places).` / `Updated A (places) and moved it to B.`, where places
  name changed and added lines as they are now and removed lines by their
  numbers before the call (`line 13`, `lines 4, 12-13; removed line 7`, `removed
  lines 2, 5-6`, `now empty`, five places at most and then `and N more places`).
  Removals name their earlier numbers because no current line shows them.
  Coverage:
  `test_apply_patch_reports.py::test_a_clean_change_names_its_places_and_shows_only_removals`.
- The changed regions as they are now, with `read`-style gutters (touching
  regions merge; more than two keep first and last plus an omission line; long
  lines show a window with an `N:C|` gutter), follow such a line (`Updated X
  (places):`) only when the written text differs from what the call sent: a copy
  tolerance note (lines that differed or were kept, respelled words), a note on
  lines read as added (step 8 below) or on a closing `*** End of File` (the
  engine marks these as `DeviationNote`), or the note on which occurrence of a repeated
  text was changed, when the change removed lines, and in partial results.
  Removed lines leave nothing to name in the file as it is now. Agent-eval
  2026-10-10, removing three lines, a reread or search in a call of its own:
  GPT-6 Luna 2 of 4 runs without the region, 0 of 4 with it (it checked inside
  its test command instead); DeepSeek V4.1 Flash 4 of 4 without, 2 of 4 with
  it, so the region does not stop DeepSeek's reread. A clean change that only changes or adds lines shows no region: the
  Agent wrote that text itself, and Agents reread or ran `git diff` after a
  region anyway (real Sessions since 2026-09: 13-22% reread a file within three
  calls before changing it again, with the region shown; user decision
  2026-10-10). Notes (recoveries, metadata drift) and syntax warnings follow as
  `Note:` / `Warning:` lines. Syntax warnings compare the initial and final
  Tool-written text; a created or fully replaced file reports any final syntax
  error, even when the original was invalid. A warning for an error this call
  caused is followed by the lines around the error (two above and below, numbered
  like `read`, with a caret under the reported column when the parser names one;
  `syntax_check._with_excerpt`); a warning for an error the file already had shows
  none, since that error is not the Agent's to find now.
- Mixed outcomes are a success led by counts (`N of M changes
  applied; K did not.`, or `fully applied; K only in part` when a write was
  incomplete; `edit` leads with its edit numbers instead, `tools/edit.md` ->
  Results) and the instruction to resend only unfinished changes; each failure
  follows as `Failed:`/`Skipped:`/`Incomplete:` with its `where` (path plus
  `hunk N`/`edit N` when several). All-failed calls use the ordinary failure
  envelope (`all_changes_failed` for several), opening with `No file was
  changed.` (several: `None of the N changes was applied; no file was changed.`)
  before the same failure text, as every error says first what was applied
  (user decision 2026-10-12); no applied effect hides behind a failure envelope.
- `unchanged` covers verified no-ops (`already has this content`, `already
  contains this change`, `already has that name`, identical old/new text) and
  changes that cancel out (`cancel each other out`). It never substitutes for
  failed or uncertain operations. The WebUI marks a successful call partial when
  its display records an `error` notice (`toolDisplayRecordsError`); a partial
  result recorded before 2026-10-10 has no such notice and shows as success.

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
  In a patch without `*** End Patch` whose last line is `*** End of File`, that
  line may only close the patch (`_Hunk.closing_eof`): its hunk is placed at the
  end of the file where its lines match there, and else as if the marker were
  absent, with a note naming the changed line (`_CLOSING_EOF_NOTE`). Evidence
  (replay of 7881 calls, 2026-10): one Model (mimo-v2.5) ended all 41 of its
  patches so; 35 changed the end of the file, 6 were refused although their place
  was clear, and 5 of those now apply at that place (checked by hand; 1 fails for
  another reason).
  Missing context prefixes and omitted `@@` are accepted; unknown operation
  headers, unframed prose, and body text after End Patch without a new file header
  are rejected. Explicit `@@` hunks following Move File use Update-plus-Move semantics.
- Only V4A is read (`_parse`). Without any file header, the body updates the
  `path` field's file. Otherwise a line before the first header fails
  `invalid_patch` with the `before_header` wording, which shows the V4A form;
  unified/git diffs and SEARCH/REPLACE blocks fail this way
  (`test_other_patch_dialects_fail_naming_the_patch_form`). Numbered unified
  hunk headers (`@@ -12,3 +12,4 @@`) are read as `@@` hints and fail
  `context_not_found` unless the file holds that line.
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
- `_edit_engine.py` places a V4A hunk through a fixed sequence; the first step
  that places it wins, and several matches at a step never fall through to a
  later, looser one (`apply_hunks`; each hunk sees the text the hunks before it
  produced):
  1. Runs of added lines that all carry read gutters lose them
     (`clean_additions`, gutter rule below).
  2. `@@` hints select successive whole lines, each after the one before,
     through precise matching only. A hint that is not found fails
     `context_not_found`; the hunk is never retried without it. A hint that
     occurs several times is read from its first occurrence, as in Codex, but
     only while the hunk's lines then match once through a precise strategy (not
     `copy_match`, not an already-applied post-state, not an addition-only
     hunk); otherwise it fails with `ambiguous_context`. That one place is right
     whichever occurrence was meant. Evidence: in all 4 `ambiguous_context`
     failures of one Swarm, the hunk's lines occurred once in the file
     (Sessions, 2026-09).
  3. An addition-only hunk inserts after the last hint, or appends without one.
     Exact adjacent content after a hint makes a repeated nonblank insertion a
     no-op; unanchored appends always append, because an existing suffix cannot
     distinguish a retry from an intentional repeated line.
  4. The unchanged and removed lines match precisely as whole lines from the
     start of the last hint's line, so the final hint may also be the first
     context/removal line. `*** End of File` restricts matching to EOF; a hunk
     is never retried without the marker, except one that only closes the
     patch (framing above).
  5. After a miss, read gutters are stripped from the hunk (rule below); then
     surplus blank context at the hunk's edges is dropped (rule below).
  6. A change already in the file is a no-op (already-applied rule at the end of
     this section), never under a repeated hint.
  7. `copy_match.match_copied_edit` places old lines copied with errors, unless
     the path is precise-only after an earlier failure or the new text is
     already present (`copy_match` rule below).
  8. Unchanged lines between `+` lines that the file holds nowhere are read as
     added lines missing their `+` (`_unmarked_additions`), as written: a
     space-prefixed line keeps that space as indentation, since the `+` was
     dropped, not replaced. Empty lines written without a prefix between `+`
     lines are then added too. The remaining unchanged and removed lines must
     hold at least 4 characters and match once, precisely; a re-read line
     beyond them must not resemble (ratio >= 0.6) the file lines next to the
     match on that side, where it can be a file line copied with errors. Like
     step 7, step 8 does not run on a precise-only path: after an earlier
     failure in the file, a line the file lacks can be one the failed change
     would have written. A note names the line or the count (`_UNMARKED_NOTE`, `_UNMARKED_LINES_NOTE`,
     `_UNMARKED_BLANK_NOTE`). Evidence (replay of 7881 calls, 2026-10): 60
     calls refused in whole or part now apply (2 through empty lines alone).
     The re-read lines are continuation lines of a wrapped call or string, or
     comment and docstring lines of the added block. Of 126 such lines in the
     first 52 calls, the Agents' next calls wrote 42 without `+` again, 45 with
     `+`, and reworded the other 39 (reviewed by hand).
  9. Lines a precise strategy finds several times are ordered as in Codex:
     after a hint, the first occurrence from the hint on is changed; a hunk
     without hints takes the first occurrence from the line where the same
     Update's last completed change ended (`_Batch.change_ends`,
     `replace_fuzzy(first=True)`), and fails with `ambiguous_match` when none
     follows. A note names the count and the changed line. Without a hint or an
     earlier change of that Update, and for `copy_match` passages, several
     occurrences stay `ambiguous_match`. Evidence (Sessions since 2026-09-01,
     89 ambiguous hunks): the Agent's later successful edit targeted the first
     occurrence after the hint in 16 of 16 and after the previous hunk in 44 of
     45; bare first hunks meant the file's first occurrence only 11 of 15
     times, too few to choose silently.
  `_splice` then writes the placed hunk (byte, indentation and typography rules
  below).
- The rebuild (2026-10) dropped every recovery that re-read a hunk as something
  it did not say: unprefixed lines re-read as `+` lines, retries without the
  `@@` lines or without `*** End of File`, replacement of part of a line, escape
  decoding, ordering by identical twin hunks, and the note on insertions that
  resemble their hint. Replaying 8883 recorded calls (2026-10), re-reading
  unprefixed lines carried 351 calls, and 46 of them wrote content that
  conflicts with the file the Session's later calls produced; the other dropped
  recoveries carried 8 such calls. They return only with replay evidence. Step 8
  and the closing `*** End of File` (framing above) are such narrow returns:
  step 8 re-reads only lines between `+` lines that the file lacks, and only
  after every other step missed.
- Lines a hunk names that the file holds exactly, but not where its markers put
  them, fail with that reason instead of closest text equal to the patch: not at
  the end of the file under `*** End of File`, `eof_not_found` (`the file does
  not end with the lines before *** End of File. That marker changes only the
  end of the file; to change these lines where they are, leave it out.`); above its `@@` lines,
  `not_after_hint` (`the lines to replace are not after the @@ line "..."; they
  are at line N, above it. After @@, put a line above them, such as the first
  line of the enclosing function, or leave @@ empty.`). Both still show the
  candidate excerpt. In the 2026-10 replay, these reports replaced 5 and 2
  results that the dropped retries had applied. An addition-only hunk whose last
  `@@` line is not the file's last line fails `eof_after_hint` (`*** End of File
  adds the lines at the end of the file, but the @@ line "..." is not the last
  line. To add them after that line, leave out *** End of File; to add them at
  the end of the file, leave out the @@ line.`) with that line's excerpt; it said
  `No similar text is in the file` before, because it searched for the added
  lines.
- Match errors show bounded candidate excerpts with `read`-style gutters
  (`The closest text in the file, lines A-B:`); touching or overlapping excerpts
  merge. Missing targets use similarity-ranked diagnostics plus `First difference,
  line N: the file has '...' where the patch has '...'` (`where old_string
  has` for `old_string` edits). Quoted text in these reports, in the `line_join`
  refusal and in notes holds the characters as the file or the call has them
  (`_patch_syntax.quoted`, not `repr`): a backslash or quote stays single, and
  only a character that shows nothing and is not whitespace is written as an
  escape (`​`); text in `'` uses `"` when it holds `'` but not `"`
  (`edit.md` -> Agent-facing text). An `old_string` of several lines may start
  inside its first file line and end inside its last: those lines differ only
  when the file line does not end, or start, with them (`first_difference`
  without `whole_lines`; patch lines are whole lines, and one that is only part
  of a line keeps its `is only part of` report). Such a copy matches only with
  the whitespace at its inner line breaks (the precise strategies accept other
  indentation only for copies of whole lines), so a line differing there alone,
  such as a continuation line that lost its indentation, is the first
  difference, reported with `; only the whitespace at the start of the line
  differs.` (`end`; the `spacing` field and the per-line test
  `_paired_difference` are shared with `line_differences`, `extensions/swarm.md`).
  It named the mid-line first line before, whose start the Agent then copies
  into its retry while the lost indentation stays (Swarm Wiki, Sessions 2026-10;
  the edit report had the same fault). Lost indentation stays a mismatch (user
  decision 2026-10-12). Candidate windows come from the three longest
  copied lines and from distinctive copied lines (at most 3 occurrences) the file
  holds exactly; a window at least 2 such lines place is shown even below the
  0.60 similarity floor (`find_closest_candidates`). The line-by-line comparison
  (`fuzzy_match.first_difference`; Swarm Wiki edits name every differing line
  with `line_differences`, which aligns the same way) starts at the
  first copied line with a word the file holds near the window, because a window starts wherever its best-matching lines
  put it, shifted by every line the copy added or dropped before them. Closing
  quotes and brackets occur too often to align by; when the file holds no other
  copied line there, the nearby file line most similar (>= 0.50) to the start of
  the first copied line starts it (a reworded copy resembles its line; a copy
  that joins lines starts like the first of them, which whole-line similarity
  missed once in a replay), else the window start does. Similarity compares
  words first and characters second: `_EQ_OPS = frozenset({...})` shares more
  signs with the file's `_RANGE_OPS = {...}` than with its own `_EQ_OPS = {...}`,
  and the character ratio named the wrong line (Swarm Run 2026-10; the replay of
  227 first differences in failed calls changed only that one). Evidence:
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
  `new_string`, as after a deletion). An `old_string` replacement whose change
  the file already holds, by the evidence `tools/edit.md` -> Results names,
  changes nothing instead of failing (`_new_text_in_place`; an `apply_patch`
  step then reports `already contains this change`). Without candidates the text names
  the `read` call. A first difference for a one-line copy is shown at the closest
  candidate's line; longer copies are aligned by the lines the file holds.
  An `old_string` whose words are all at exactly one place of the file, with
  other whitespace between them (`rewrapped_place`), shows that place (at most 8
  lines) as its candidate, and its first difference starts there. When no
  whole-line window resembles an `old_string`, the closest passages anywhere in
  the text show instead (`find_closest_passages`): both sides compare with each
  whitespace run as one space, so part of a long line and a line broken in other
  places are found. The copy's rarest words place them; at most 40 places are
  compared with its first 400 characters, and at most 3 passages show, each with
  >= 0.60 similarity and within 0.1 of the closest. Diagnostic only, like the
  whole-line candidates. A place on a line longer than 240 characters shows the
  240-character window around it, numbered `line:character|` like an ambiguity
  window (`_place_excerpts`), and the first difference compares the copy's
  first line from the place on (`first_difference(character=...)`), unless the
  copy starts with a line break; it is compared like a mid-line first line
  above (the rest of the file line must end with it), so a placed copy and a
  whole-line candidate name the same kind of difference. Evidence: 17 of 38 `old_string` failures in
  637 replayable `edit` calls said `No similar text`; 5 now show the place
  (part of a long JSON or prose line, a one-line JSON file copied
  pretty-printed, a line rewritten since), the other 12 held text the file
  lacks (Sessions 2026-10-07 to 2026-10-12). Whitespace between JSON tokens
  stays a difference for matching: 1 of the 637 calls differed only there, and
  its new text, written as sent, would have put pretty-printed lines into a
  one-line file; the report now names the place and the differing `}`. A first
  difference that is a line break in another place carries `break` (`file`,
  `copy`, or `start` for a copied line that starts a line the file continues)
  with the text on both sides, and the report says so (`the file breaks this
  line after '...'; old_string continues it with '...'`) instead of quoting the
  whole line. Only a copy that starts with a line break, or a patch line,
  claims to start a line; an `old_string`'s first line can start inside one. Ambiguity reports the winning match's actual locations
  (including section offsets) under `Where it occurs:`, not guessed alternatives.
  `AmbiguousFuzzyMatch.spans` carries each match's offsets. An occurrence on a
  line of at most 240 characters shows that line and its neighbors; one on a
  longer line shows a 240-character window of that line starting 80 characters
  before it (at the line start within its first 160), numbered
  `line:character|` as `read` continues a line, as its own excerpt; windows on
  one line never overlap, and a continuation starts after the text they show
  without a gap. A line holding several occurrences, or a long line, also names
  their characters in the message (`line 1, characters 82 and 3021; line 4`,
  `_place_list`). Evidence: 4 ambiguity reports in 3 `edit` calls named 2
  places as `(line 1)` of a one-line JSON file and showed only its first 240
  characters, neither place (Swarm run 2026-10-12). At most three excerpts show.
  These excerpts never authorize a write.
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
- A hunk that is not found leads with `X: the lines to replace were not found.`;
  a hunk without `-` lines replaces nothing, so its lead is `X: the context
  lines around the + lines were not found.` (`context_lines_not_found`, same code
  `text_not_found`).
- When the reported first difference is an unchanged line that only unchanged
  lines separate from `+` lines on both sides, or one right next to a `+` line,
  the report adds `That patch line has no + prefix, so it must already be in the
  file there; if it is new, start it with +.` (difference key `unprefixed`),
  since identical retries followed the bare difference. Session shape: new lines
  written without `+` between or after `+` lines.
- A match failure (`text_not_found`, `context_not_found`, `ambiguous_match`,
  `ambiguous_context`) on a file changed after this Session's last read appends
  `X changed after this Session last read it; read it again before resending.`
  (`_STALE_FAILURE`); the patch was likely written against old content.
- After any text-entry failure on a path, remaining hunks and `old_string` edits
  on that path (the later edits of an `edit` call too, inside its one step:
  `apply_hunks` with `failures`) allow only unique precise matches (no `copy_match`), preventing
  tolerance from silently satisfying a failed earlier precondition. The failure
  also drops that path's `change_ends`, since the failed target still holds its
  old lines; only a hint then orders occurrences. Other files retain normal
  tolerance.
- `fuzzy_match.replace_fuzzy` owns precise matching: exact, normalized (newline,
  Unicode, typography), line-trimmed, and whitespace-normalized, in that order.
  Patch-only options require whole-line matches and constrain EOF. The matcher
  retains its default substring mode for direct callers. Its
  `fold_for_search` is a separate, looser read-side fold for web_fetch `find`
  (`tools/web_fetch.md`) that edits never use. No strategy's span
  starts or ends between the CR and LF of one line break: an old text starting
  with a line break replaces the whole CRLF instead of leaving a lone CR. Precise matches win;
  ambiguity at a winning strategy never falls through to a looser strategy
  (`first=True` takes that strategy's first match instead). With
  `rewrapped=True` (substring `old_string` replacements only: `edit` and
  `apply_patch`'s `old_string` shapes), a last strategy matches an old text
  whose words (non-whitespace runs) are all the file's, in order, with line
  breaks in other places: between words a space and a line break with any
  indentation stand for each other, a blank line must be in the file too, a
  leading or trailing line break must start or end a file line, leading
  indentation alone stands for the start of a line or a space or tab inside one
  (Swarm 2026-10-13: an indented copy of a passage in a one-line JSON file; a
  start after indentation counts once, from the line's start), at least one
  break must fall elsewhere, and the old text needs 4 words. It never serves an
  edit that changes only whitespace, since such an edit is about the breaks the
  old text misstates. It is ambiguous like the others; the replacement is
  reindented from its first line only (`lines_correspond=False`).
  Ambiguity counts every occurrence, including overlapping ones (repeated
  closing-brace lines) and ones following a filtered partial occurrence, for
  hunks and `@@` anchors alike; `replace_all` still replaces leftmost
  non-overlapping spans. The report stays linear in the file size: the lines
  of all occurrences come from one pass over the line breaks, and `replace_all`
  builds its result once (counting each occurrence's line from the file start
  took minutes on 100,000-line files; `test_fuzzy_match.py` guards it).
- In a file with CRLF line breaks, the normalized step first searches the old
  text with CRLF breaks (`_found_with_crlf`) and takes those spans when the
  normalized file text, folded without a span map, holds no more matches;
  otherwise it maps every character as before. Both give the same spans, so the
  result and its `normalized` strategy (reindent, typography restore) are
  unchanged. Replacements ask for the file's indentation unit only when they
  convert indentation. Probe: an LF `old_string` on a 1 MB CRLF file matched in
  about 16 instead of 400 ms and the whole `edit` call took about 330 instead
  of 810 ms (4 MB: 60 instead of 2200 ms matching). Guard:
  `test_repeated_text_is_ambiguous_unless_every_occurrence_is_replaced`.
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
  repeat the moved words. Lines the edit replaces may hold no more blank lines
  than their copy: a blank line holds nothing a copy could miss, so a copy that
  breaks a line in two (`return` and `1` for `    return 1`) would otherwise take
  the blank line below as its second line and remove it (found 2026-10-12; the
  passage does not qualify, and the `old_string` report shows the line-break
  first difference). A misspelling is a word of
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
  spacing count as one. Spaces or tabs a change writes at its edge replace the
  file's spacing at that seam, so `1,2` -> `5, 6` over `1, 2` gives `5, 6`, not a
  doubled space. When the winning passage cannot take the change,
  nothing is applied elsewhere and the hunk fails `text_not_found`. Warnings
  name each replaced line and each kept line that differed (`... was edited
  anyway; it read:` / `... was left as it reads:`, long lines from just before
  the first difference) and each respelled word (`copy_warnings`). A replaced
  line is numbered as it was; a kept line where the edit leaves it, so the
  number matches the region shown above the note. Tests:
  `test_copy_match.py`, plus the
  patch, field, Skill and Wiki suites.
- Only changed lines are emitted from the replacement; context lines keep their
  actual original bytes.
- Lines a change adds before a definition whose decorators, annotations or
  attributes lie directly above the matched lines go above those decorators
  (`_decorated_insert.place_before_decorators`, called for every placed text
  replacement and hunk). The Agent copied the definition's line without them,
  so its literal effect hands the decorators to the added code or breaks the
  syntax: replay of 10063 recorded edit and apply_patch calls (2026-10) found 2
  such calls, both wrong as written (a `@staticmethod` split from its method by
  a new method above it, Swarm Run 2026-10, exact and tolerated copies alike; a
  `@contextlib.contextmanager` left on a comment block). Decorators the added
  lines end with stay directly on the definition; lines that only add
  decorators, comments or blank lines stay as written. The definition is found
  again in the replacement by its name at its indentation (a changed signature
  counts; a rename or a name held twice there leaves the change as written);
  only file types with known decorator syntax take part (`@` for Python,
  JS/TS, JVM languages, Dart, Swift; `#[` for Rust and PHP; `[` for C#). A note
  names where the decorators and their definition are now. Tests:
  `test_apply_patch_matching.py::test_lines_added_before_a_decorated_definition_keep_its_decorators`.
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
  lines keep their offset instead of every space becoming a file level. Lines
  that read alike decide an indent's mapping before lines that differ, and a
  pair the decided mappings contradict is left out: a copy that joins a line to
  the next one (`{"quote": ...` for `{` and `"quote": ...`) carries the indent
  of other text (Swarm run 2026-10-12: such a copy wrote the closing brace and
  the added lines one level too deep). Test:
  `test_copy_match.py::test_line_endings_and_indentation_follow_the_file`.
- Read-output gutters recover after raw matching misses, including single lines,
  mixed raw/numbered locators, and stale line numbers. Their stripped contents
  must identify a unique whole-line target; line numbers never resolve ambiguity.
  A run of added lines is recovered only when every line carries a gutter;
  complete but nonconsecutive or continuation gutters are rejected
  (`line_numbered_content`). Only unchanged and removed lines can make a miss a
  gutter error. Two or more gutter-shaped lines among other added lines are
  content, such as `N|value` data rows, and are written as sent with a note
  quoting one of them. Literal matching against gutter-shaped existing content
  takes precedence over correction. An `old_string` whose every line carries a
  gutter is retried without the gutters, as whole lines, after its substring
  match misses.
- Patch-only typography normalization also recognizes expanded em dashes and
  ellipses, minus signs, and Unicode spaces. Precisely equivalent changed lines
  preserve original glyphs only in unchanged portions; explicit glyph changes
  and merely similar preimages do not trigger restoration.
- Escapes stay literal: approximate matches cannot introduce doubled
  quote/backslash escapes absent from the actual target.
  Surplus blank boundary context can be dropped after the full locator misses.
  Blank lines explicitly marked for deletion remain meaningful operations.
  A blank last context line matches the empty line past the file's final line
  break; `+` lines after it end with that line break, so the file keeps it.
- Empty lines written without a prefix between `+` lines, outside a hunk's
  nonblank unchanged and removed lines, are added lines
  (`_patch_syntax._read_added_blank_lines`). Read as unchanged, they matched the
  file's blank lines beside those lines and moved them into the added block, so
  the code after the block lost its blank-line separation. Evidence (replay of
  7881 calls, 2026-10): 45 such lines in 35 calls, none meant as unchanged; 27
  applied calls had lost one or two blank lines this way, and 5 refused calls
  now apply. Between unchanged lines, such an empty line stays unchanged and
  step 8 adds it only when that reading misses; an empty line written with its
  space prefix always stays unchanged (all 8 between `+` lines were the file's
  blank line).
- Context-only blocks before another `@@` become ordered precise locator hints
  for that next hunk, including multiline context. A missing anchor fails like a
  missing hint, and repeated anchors follow the hint rule above (step 2). A multiline block that is not found fails `context_not_found`
  with the `context_block_not_found` wording (`the lines of the @@ block "..."
  above the lines to replace were not found together`, naming the block by its
  first line), the closest text and its first
  difference. The former report named the block's first line as not found,
  although the file had it (1 failure, Sessions, 2026-09). Duplicate matches after
  the anchor resolve to the first (step 9 above). Anchors do
  not leak into subsequent edits or files.
  An entirely context-only patch fails with `no_changes`, says that without a
  `-` or `+` line every line stays unchanged, and
  states the replace/insert-above rule. For each context-only block (at most 3)
  whose lines are found, it shows where: `The unchanged lines match X line(s)
  N-M:` with the matched lines and 2 lines around them numbered like `read`, or,
  for several occurrences, `occur K times` with the first 3 excerpts and asks for
  more unchanged lines. When the block's lines are not found, it names the first
  line the file lacks and that a new line starts with `+` (new lines written
  without `+` parse as unchanged lines).
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
  are line content). New lines adopt the style most of the file's lines end
  with (`fuzzy_match.dominant_line_ending`; a tie goes to CRLF, then LF), else
  LF; a stray CRLF or lone CR in an LF file no longer sets the ending of every
  added or replacing line. An Add of a new file ends all its lines as most lines
  of its text do. Explicit no-newline markers apply only at EOF.
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
  presentation-only counts: added and removed lines, the applied edits (each
  hunk, Add, Delete and Move; for `edit`, each edit that was not skipped; `write`
  counts none) and, only when several files changed, the files, a move counting as
  one. Each reported file also records its diff
  of actual before/after text (`ToolContext.add_display_file_change`, display
  `file_changes` block); the line counts are that diff's, so a replaced file counts
  its changed lines, not its whole content. A call that changed no file records no
  counts. Updates reuse syntax-delta warnings; Add uses full-file syntax warnings.
- The display declares detail blocks: after the diffs, `patch_result` records one
  notice per file note (`info`), syntax warning (`warning`, its first line only:
  the user sees the change itself) and net-effect note,
  and one `error` notice per failed change with its message alone - not the
  excerpts, difference and `read` continuations the Model's recovery needs. The user reads the
  Model's result text only in the raw call disclosure.
- The handler uses the shared cancellation-shielded Tool worker boundary so
  an in-flight mutation settles before cancellation returns.

## Agent-facing text

About 55 tokens (`python -m scripts.tool_lab definitions` estimate), against about
315 before 2026-10-05. A sentence stays only when a fresh Agent would often make a
failing call without it (user decision 2026-10-04). Removed, with what carries
each case instead: the example and the prefix, `@@`, whole-line and Add File rules
(trained V4A semantics; Add File on an existing file overwrites in Codex too), the
replace/insert advice and the rule that every block needs a change (`no_changes`
names both and shows where the lines are, or the line the file lacks without `+`),
batching and partial semantics (a partial result says what was applied and what
to resend), and the path forms (both are accepted). Pure `+` blocks after an `@@`
hint are inserted after that line, where Codex appends them at the end of the
file; the result names the lines they now occupy. `Add File also replaces an
existing file.` on `patch` was removed again: with it, 8 of 8 GPT-6 Luna
replacements still went through Delete File plus Add File (agent-eval
2026-10-10), which is harmless since both land in one call.

| Text | Reason |
|---|---|
| `Edit, create, replace, delete or move files with a patch.` | Names every effect, so deletes and renames do not go through the shell, which bypasses read stamps and change tracking (F1). `replace`: GPT-6 Luna replaced a README with Delete File and then Add File in two calls plus a verification read (agent-eval 2026-10-10). |
| `patch`: `Patch text from *** Begin Patch to *** End Patch.` | Pins `patch` to V4A by its frame, so a Model does not send a unified diff (F2). |
| Syntax warning excerpt (shared with `edit` and `write`): `Warning: Syntax check failed after this edit: SyntaxError: '(' was never closed (line 2, column 12)` followed by `1| ...`, `2| ...`, a caret line and `3| ...` | Real Sessions (2026-10) read 30-45 lines after a warning to find the line the error names; the excerpt shows it in the result. Only the error's own lines are shown, not more, so a result stays short. |
| Decorator note (shared with `edit`): `The added lines went above line 13 (@staticmethod), not between it and the definition at line 14 it belongs to.` | The written text differs from the call, so the Agent learns it and gets the region shown; without the note it would see its lines below where it put them and could "fix" them back between decorator and definition. Line numbers name the decorators and the definition, not the added lines, whose numbers the `Updated X (lines ...)` header can count differently when an added line repeats a decorator. |
| `context_lines_not_found`: `X: the context lines around the + lines were not found.` | A block that only adds lines was reported as `the lines to replace were not found`, which names lines the block does not have and points the Agent at text to replace instead of at its context lines (review of a Swarm Run, 2026-10). |
| `file_not_read` lead (shared with `write`): `X already exists and was not read with read in this conversation, so it was not replaced.` | Names the real state: an Agent that saw the file through the shell or a search has no read stamp, and the former `this Session has not read it` contradicted what it remembered (review 2026-10-10). |
| Step 8 notes: `The line '...' between + lines has no + prefix and is not in the file; it was added as a new line.` / `N lines between + lines have no + prefix and are not in the file, such as '...'; they were added as new lines.` | The call was read differently from its prefixes, so the result says how (Tolerance: report every interpretation). It names a line, so the Agent can check it in the excerpt and need not reread the file. In the Agents' next calls, 42 of 126 such lines were written without `+` again (replay 2026-10); the note names the cause. |
| `The empty line between + lines has no prefix and, read as unchanged, does not match the file; it was added as a new line.` (and the plural) | Same reading for empty lines only; it names why the line counts as added, since an empty line is in the file elsewhere. |
| `The patch ends with *** End of File and has no *** End Patch, so that line was read as the end of the patch, not of the file: the change was made at line N.` | The marker asked for the end of the file and the change landed elsewhere; the line number lets the Agent see where without a read. Shown only when text follows the change. |
| `eof_not_found` next step: `That marker changes only the end of the file; to change these lines where they are, leave it out.` | The error named the mismatch but no next step (Errors: three parts); the lines are in the file, so leaving out the marker is the call that applies the change where the excerpt shows them. |
| `eof_after_hint`: `*** End of File adds the lines at the end of the file, but the @@ line "..." is not the last line. To add them after that line, leave out *** End of File; to add them at the end of the file, leave out the @@ line.` | Replaces `the file does not end with the lines before *** End of File` plus `No similar text is in the file`, a misleading cause for added lines, which are never in the file. Both next steps are safe as written. |
| `context_block_not_found`: `the lines of the @@ block "..." above the lines to replace were not found together.` | Names the block by its first line: a patch can hold several `@@` blocks, and without candidates nothing else in the error said which one failed. |

## Verification

- `tests/core/tools/test_apply_patch_calls.py` covers the pinned definition,
  display metadata (including the file diffs and their shared line budget), patch spellings and wrappers, and other harnesses' shapes
  (Edit, MultiEdit, Write, text-editor and MCP field names)
  through production dispatch, including empty-text edits and refused open,
  unknown or conflicting calls. `scripts/tool_lab/cases/files.json` holds the Model-visible
  result cases.
- `test_apply_patch_parsing.py` covers framing, concatenated frames, malformed
  patches, Add syntax repair, other patch dialects and move headers.
- `test_apply_patch_matching.py` covers the engine's steps: gutters, copied text
  with misspellings, code targets, typography, context hints and anchors that
  do not place a hunk, EOF markers (also one that only closes the patch),
  whole-line matching, empty lines of an added block, lines between `+` lines
  written without `+` (step 8), ordering of repeated lines and changes already
  in the file.
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
- `test_edit_tools.py` covers `edit` and `write` on the same pipeline
  (`edit.md` -> Verification).
- Existing fuzzy-match, file-state, Runtime and Provider-schema
  tests cover the shared boundaries.
  `tests/core/providers/test_ollama_cloud.py` verifies intact patch arguments through
  Cloud response normalization and Chat ingestion.
- `python -m scripts.probe_provider_tool_call --scenario apply_patch` uses the
  production registry and disposable files. Its matrix separates natural batching tasks
  (no imposed call count or prebuilt arguments), exact edge/invalid requests,
  and continuation from an actual partial Tool Result with candidate excerpts.
  It vets targets through `patch_targets`, rejects imports from a different
  checkout, checks file effects, the outcome (`edit_outcome`) and expected result text
  (`mentions`), and verifies that recovery avoids replaying a completed append. Natural
  tasks also cover insertion, EOF targeting, and recovery from missing/ambiguous
  hints. Exact cases pair move-metadata recovery with conflicting destinations and
  literal move-shaped text. Probe tests reject scope escapes and false completion.
  The probe supplies Adapter-owned request context for gateway routing. Its matrix
  includes missing Add prefixes, conflicting syntax and context-only recovery.
