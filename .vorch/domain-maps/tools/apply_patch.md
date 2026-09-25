# Apply Patch Tool

Applies ordered V4A file operations. It replaces the archived `edit` and `write` Tools (see `edit.md` and `write.md`).
Add File creation-or-replacement is a vBot extension to the V4A-style interface.
`core/tools/apply_patch.py` owns the in-memory plan, filesystem execution, results, and display metadata. Its internal `_patch_syntax.py` owns V4A parsing and parsed operation values; `_patch_hunks.py` owns matching and applying one hunk to current text (including patch recoveries); `_patch_entries.py` owns entry snapshots, Delete/Move entry resolution, and entry renames; `_change_preview.py` owns bounded before/after previews.

## Contract

- `register_apply_patch_tool(registry, *, file_state)` registers `apply_patch`
  in the `files` family with one required `patch` string. It is an ordinary
  Provider-neutral function Tool, not a Provider-native patch operation. The
  open model-facing schema is backed by handler-owned unknown-field validation.
- The owner-selected argument repair accepts `input` as the patch-text alias,
  ordinary field formatting and shared call wrappers. Equal aliases coalesce;
  conflicting aliases (including placeholder text) and unsupported fields fail
  before mutation. Patch contents remain literal. The canonical schema stays `patch`.
- The definition states that a patch can contain one or more edits across one or more
  files, without prescribing how many to combine. One example combines three replacement/insertion
  edits in two files. Each text edit supplies its changed lines and locating context
  together; a context-only call cannot select a location for a later call.
  Further guidance covers Add/Delete/Move headers, insertion-after and EOF targeting.
  Detailed continuation guidance belongs in
  results; matching errors distinguish missing/ambiguous context hints from hunk text.
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
  rewritten. Their `files` entries report the destination addition and source
  deletion; link entries carry no preview (`test__patch_entries.py`).
- The complete patch structure is parsed before mutation; unparseable framing or
  operation syntax rejects the call without writes. Once parsed, each Update
  hunk and each Add/Delete/Move is attempted in order against actual current
  bytes. A failed hunk does not prevent later matching hunks, even in the same
  file. Add -> Update and Move -> Update observe completed earlier effects.
  Parent/file overlaps reject the affected entries, not unrelated files. Add
  creates or fully replaces files. Existing targets require a current Session read
  stamp; missing/stale stamps fail with `file_not_read`/`file_modified_since_read`.
  An identical Add is a verified no-op and does not require a prior read.
  Empty Add bodies produce empty files; `\ No newline at end of file` suppresses
  the trailing newline. Replacement preserves existing line endings, UTF-8 BOM,
  and permission bits. Move destinations still reject different existing files.
- Failed creates/moves and uncertain writes block later entries touching those
  paths for this call. A failed hunk in Update plus Move leaves successful hunks
  applied at the source and skips that operation's move. Other files continue.
- Success data includes ordered entry outcomes with 1-based operation/hunk
  coordinates, resolved paths, and status; counts distinguish successful entries
  from failed/skipped/partially committed entries. Mixed outcomes return
  `status: partial` with guidance to retry only unfinished entries. All-failed
  calls use the ordinary failure envelope, retaining indexed errors/candidates
  in its message; no applied effects are hidden behind a failure envelope.
- `files` reports net completed file effects once per path, including bounded
  read-compatible previews with neighboring lines, first/last regions, long-line
  windows around changed characters, and omission metadata. Moves appear as
  destination addition and source deletion; the entry outcome retains the move
  relationship. Syntax warnings compare the initial and final Tool-written text,
  avoiding warnings caused only by intermediate hunks. A created or fully replaced
  file reports any final syntax error, even when the original was already invalid.
- `no_change` means zero net file effects, including cancelling edits.
  `already_applied` is emitted only when every entry was a verified already-present
  no-op. It never substitutes for failed or uncertain operations.

## Matching and recovery

- Canonical framing is `*** Begin Patch` / `*** End Patch`; one enclosing
  Markdown patch/diff fence, repeated leading Begin markers, omitted framing,
  and successive frames (with or without another Begin before a file header)
  are tolerated. Every frame is parsed before mutation. Body line prefixes retain
  their meaning even when content spells a patch marker.
  Missing context prefixes and omitted `@@` are accepted; unknown operation
  headers, unframed prose, and body text after End Patch without a new file header
  are rejected. Identical adjacent Update headers before a body coalesce; a
  different empty Update target is not discarded.
  Explicit `@@` hunks following Move File use Update-plus-Move semantics.
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
  content remains authoritative. `*** End of File` restricts matching to EOF.
  Addition-only hunks insert after a hint or append without a hint; exact
  adjacent content at a hint makes repeated nonblank insertions no-ops.
  Unanchored appends always append because an existing suffix cannot distinguish
  a retry from an intentional repeated line.
- Match errors return bounded raw candidate excerpts with starting file lines.
  Missing targets use similarity-ranked diagnostics; ambiguity reports the
  winning match's actual locations (including section offsets), not guessed
  alternatives. These excerpts never authorize a write. The missing-target
  guidance points at those excerpts only when the result carries any, and
  otherwise tells the Model to read the file.
- After any text-entry failure on a path, remaining hunks on that path allow
  only unique precise/normalized matches, preventing approximate matching from
  silently satisfying a failed earlier precondition. Other files retain normal
  tolerance.
- `fuzzy_match.replace_fuzzy` remains the matching owner. Patch-only options
  require whole-line matches, permit precise-only retry checks, and constrain
  EOF. The matcher retains its default substring mode for direct callers. Precise matches win;
  ambiguity at a winning strategy never falls through to a looser strategy.
  Ambiguity counts every occurrence, including overlapping ones (repeated
  closing-brace lines) and ones following a filtered partial occurrence, for
  hunks and `@@` anchors alike; `replace_all` still replaces leftmost
  non-overlapping spans.
- Newline/Unicode/typography/whitespace/indentation differences are supported
  for every hunk line. The bounded block-anchor and context-similarity
  strategies may absorb differences only in context lines: each removed (`-`)
  line must still equal its actual line up to those normalizations (user
  decision). Otherwise the hunk fails with `text_not_found` and candidate
  excerpts; similar candidates failing this rule are discarded before the
  ambiguity check (`replace_fuzzy(required_lines=...)`). Only changed lines are
  emitted from the replacement; context lines keep their actual original bytes.
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
  An entirely context-only patch fails with `no_changes` and explains that
  context locates an edit but supplies
  no insertion/removal. It never invents omitted replacement content or reports
  success. Identical old/new line sequences are no-ops only when located. A unique
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
- Existing fuzzy-match, file-state, Runtime and Provider-schema
  tests cover the shared boundaries.
  `tests/core/providers/test_ollama.py` verifies intact patch arguments through
  Cloud response normalization and Chat ingestion.
- `python -m scripts.probe_provider_tool_call --scenario apply_patch` uses the
  production registry and disposable files. Its matrix separates natural batching tasks
  (no imposed call count or prebuilt arguments), exact edge/invalid requests,
  and continuation from an actual partial Tool Result with candidate excerpts.
  It rejects imports from a different checkout, checks file effects and entry
  statuses, and verifies that recovery avoids replaying a completed append. Natural
  tasks also cover insertion, EOF targeting, and recovery from missing/ambiguous
  hints. Exact cases pair move-metadata recovery with conflicting destinations and
  literal move-shaped text. Probe tests reject scope escapes and false completion.
  The probe supplies Adapter-owned request context for gateway routing. Its matrix
  includes missing Add prefixes, conflicting syntax and context-only recovery.
