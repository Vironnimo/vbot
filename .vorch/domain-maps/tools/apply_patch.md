# Apply Patch Tool

Applies ordered V4A file operations while retaining the existing `edit` Tool.
The parser, in-memory plan, filesystem execution, results, and display metadata
are owned together by `core/tools/apply_patch.py`.

## Contract

- `register_apply_patch_tool(registry, *, file_state)` registers `apply_patch`
  in the `files` family with one required `patch` string. It is an ordinary
  Provider-neutral function Tool, not a Provider-native patch operation. The
  open model-facing schema is backed by handler-owned unknown-field validation.
- Add, Update, Delete, standalone `Move File: source -> destination`, and
  Update plus `Move to: destination` are supported. Paths use ordinary
  `ToolContext.resolve_path` semantics: cwd-relative or absolute, with resolved
  aliases sharing the same pending state and lock.
- The complete patch is parsed and simulated before any filesystem mutation.
  Later operations see earlier operations' pending contents, including Add ->
  Update and Move -> Update. Parent/file overlaps, missing sources, ambiguous
  hunks, and conflicting destinations fail before writes. Add never overwrites
  different existing bytes; an exact existing Add is an idempotent no-op.
- Success data reports `status` and the actual changed `files`, each with its
  resolved path, action, bounded before/after preview, and relevant warnings.
  An unchanged final plan returns `already_applied: true`. File moves appear
  as a destination addition and source deletion in this net-change result.
- Validation failures use the ordinary failure envelope and explicitly state
  that no files changed. Candidate diagnostics are bounded JSON excerpts inside
  the error message, preserving the shared envelope's closed error shape.

## Matching and recovery

- Canonical framing is `*** Begin Patch` / `*** End Patch`; one enclosing
  Markdown patch/diff fence and omitted framing are tolerated. Body line
  prefixes retain their meaning even when content spells a patch marker.
  Missing context prefixes and omitted `@@` are accepted; unknown operation
  headers, unframed prose, and non-empty text after End Patch are rejected.
- `@@ context` hints select successive unique whole lines at the hunk's section.
  The final hint may also appear as the first context/removal line. Multiple
  hints can narrow a section. Numeric unified-diff headers are advisory;
  content remains authoritative. `*** End of File` restricts matching to EOF.
  Addition-only hunks insert after a hint or append without a hint; exact
  adjacent content at a hint makes repeated nonblank insertions no-ops.
  Unanchored appends always append because an existing suffix cannot distinguish
  a retry from an intentional repeated line.
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
  in deterministic order and held across planning and writing. Existing-target
  Updates do not require a prior read; they match against current bytes. Content
  and mode are checked again after planning and before each mutation.
- Updates reject NUL bytes and invalid UTF-8. BOM, surviving context bytes,
  existing EOF newline state, and file permissions are preserved. New lines
  adopt the detected file style; explicit no-newline markers apply only at EOF.
  Binary files may be moved or deleted without text decoding.
- Writes reuse `atomic_write_bytes`; its optional `mode` carries source
  permissions to a move destination. Destinations are written before source
  deletions. There is no multi-file filesystem transaction: an I/O failure stops
  writing and returns `status: partial` with completed files and pending paths
  when anything changed, otherwise a failure envelope. No rollback is claimed.
  Locks cannot exclude unrelated external writers; atomic replace is not a
  portable filesystem compare-and-swap.
- Successful surviving files, including verified no-ops, receive Session read
  stamps. Metadata drift can produce a post-success warning. Text mutations
  feed the existing ChangeTracker with actual before/after contents and publish
  presentation-only line/file counts. Existing syntax-delta warnings are reused.
- The handler uses the shared cancellation-shielded Tool worker boundary so
  an in-flight mutation settles before cancellation returns.

## Verification

- `tests/core/tools/test_apply_patch.py` covers framing, matching/retries,
  byte preservation, ordered plans, failure containment, locking/cancellation,
  read stamps, statistics, syntax warnings, and generic display metadata.
- Existing fuzzy-match, edit, write, file-state, Runtime and Provider-schema
  tests cover the shared boundaries.
- `scripts/probe_provider_tool_call.py --scenario apply_patch` uses the
  production registry and disposable files. Its 16-case Luna matrix includes
  natural first-use tasks and exact edge/invalid requests; success checks actual
  files and error codes. Probe tests prevent completion claims or scope escapes
  from counting as success.
