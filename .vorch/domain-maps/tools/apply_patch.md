# Apply Patch Tool

Applies ordered V4A file operations while retaining the existing `edit` Tool.
`core/tools/apply_patch.py` owns the in-memory plan, filesystem execution, results, and display metadata. Its internal `_patch_syntax.py` owns V4A parsing and parsed operation values.

## Contract

- `register_apply_patch_tool(registry, *, file_state)` registers `apply_patch`
  in the `files` family with one required `patch` string. It is an ordinary
  Provider-neutral function Tool, not a Provider-native patch operation. The
  open model-facing schema is backed by handler-owned unknown-field validation.
- Add, Update, Delete, standalone `Move File: source -> destination`, and
  Update plus `Move to: destination` are supported. Paths use ordinary
  `ToolContext.resolve_path` semantics: cwd-relative or absolute, with resolved
  aliases sharing the same mutation history and lock.
- The complete patch structure is parsed before mutation; unparseable framing or
  operation syntax rejects the call without writes. Once parsed, each Update
  hunk and each Add/Delete/Move is attempted in order against actual current
  bytes. A failed hunk does not prevent later matching hunks, even in the same
  file. Add -> Update and Move -> Update observe completed earlier effects.
  Parent/file overlaps reject the affected entries, not unrelated files. Add
  never overwrites different existing bytes; an identical Add is a verified no-op.
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
  avoiding warnings caused only by intermediate hunks.
- `no_change` means zero net file effects, including cancelling edits.
  `already_applied` is emitted only when every entry was a verified already-present
  no-op. It never substitutes for failed or uncertain operations.

## Matching and recovery

- Canonical framing is `*** Begin Patch` / `*** End Patch`; one enclosing
  Markdown patch/diff fence, repeated leading Begin markers, and omitted framing
  are tolerated. Body line prefixes retain their meaning even when content spells
  a patch marker.
  Missing context prefixes and omitted `@@` are accepted; unknown operation
  headers, unframed prose, and non-empty text after End Patch are rejected.
  Explicit `@@` hunks following Move File use Update-plus-Move semantics.
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
  alternatives. These excerpts never authorize a write.
- After any text-entry failure on a path, remaining hunks on that path allow
  only unique precise/normalized matches, preventing approximate matching from
  silently satisfying a failed earlier precondition. Other files retain normal
  tolerance.
- `fuzzy_match.replace_fuzzy` remains the matching owner. Patch-only options
  require whole-line matches, permit precise-only retry checks, and constrain
  EOF. Existing `edit` defaults and behavior remain intact. Precise matches win;
  ambiguity at a winning strategy never falls through to a looser strategy.
- Newline/Unicode/whitespace/indentation differences and the existing bounded
  block/context similarity strategies are supported. Only changed lines are
  emitted from the replacement; context lines keep their actual original bytes.
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
- Context-only intermediate hunks are ignored; an entirely context-only patch
  fails. Identical old/new line sequences are no-ops only when located. A unique
  precise post-state with at least four shared non-whitespace context characters
  permits an already-applied retry before approximate matching. A missing
  deletion/move source remains an error, not inferred proof of prior execution.

## Mutation invariants

- One Runtime's shared FileReadState locks cover every resolved path, acquired
  in deterministic order and held across the call. Each entry is planned on its
  own; existing-target Updates need no prior read. Bytes and mode are checked
  after planning, before mutation, and after completed writes. Later entries
  detect drift from earlier observations and leave affected paths alone.
- Updates reject NUL bytes and invalid UTF-8. BOM, surviving context bytes,
  existing EOF newline state, and file permissions are preserved. New lines
  adopt the detected file style; explicit no-newline markers apply only at EOF.
  Binary files may be moved or deleted without text decoding.
- Writes reuse `atomic_write_bytes`; its optional `mode` carries source
  permissions to a move destination. A destination is written and checked before
  its source is deleted; both paths are rechecked before deleting the source.
  A move that wrote its destination but could not delete its source reports
  completed/pending paths and blocks follow-up entries on both. An observation
  failure after writing also retains the completed effect in a partial result.
  No rollback is claimed. Locks cannot exclude unrelated external writers;
  checks plus atomic replace are not a portable filesystem compare-and-swap.
- Successful surviving files, including verified no-ops, receive Session read
  stamps. Metadata drift can produce a post-success warning. Text mutations
  feed the existing ChangeTracker with actual before/after contents and publish
  presentation-only line/file counts. Existing syntax-delta warnings are reused.
- The handler uses the shared cancellation-shielded Tool worker boundary so
  an in-flight mutation settles before cancellation returns.

## Verification

- `tests/core/tools/test_apply_patch.py` covers framing and matching;
  `test_apply_patch_operations.py` covers byte preservation, ordered plans,
  read stamps, statistics, syntax warnings, and display metadata;
  `test_apply_patch_transactions.py` covers partial effects, failure containment,
  locking, cancellation, and guarded retries.
- Existing fuzzy-match, edit, write, file-state, Runtime and Provider-schema
  tests cover the shared boundaries.
- `python -m scripts.probe_provider_tool_call --scenario apply_patch` uses the
  production registry and disposable files. Its matrix separates natural batching tasks
  (no imposed call count or prebuilt arguments), exact edge/invalid requests,
  and continuation from an actual partial Tool Result with candidate excerpts.
  It rejects imports from a different checkout, checks file effects and entry
  statuses, and verifies that recovery avoids replaying a completed append. Probe tests reject scope escapes and false completion.
