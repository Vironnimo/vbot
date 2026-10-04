# File Search Tool

Read when changing file/content search, search options, native engine provisioning,
search permission consolidation, or result continuation.

## Ownership and Interface

`search_files` is the only file and content search Tool. The definition sends
file and content discovery here instead of shell rg/grep/find/ls. ripgrep owns
traversal, ignore rules, hidden-file and binary handling, globs, types and matching.
vBot owns call interpretation, roots, ordering, paging, rendering and diagnostics.
Its private modules are implementation units of the Tools owner, not public
services:

- `core/tools/search_files.py`: registration, the owner normalizer
  (`normalize_search_arguments`), `interpret_search_call` (the provider probes use
  it too), root resolution, scopes, orchestration (`_search`), the handler, the
  display, the help text and the definition.
- `core/tools/_search_query.py`: `interpret` turns the named fields and `args`
  into one `SearchQuery`, with mode, patterns, roots, globs, the rg arguments,
  context, page and notes.
- `core/tools/_search_execution.py`: bounded, cancellable ripgrep children
  (`native_lines`), the counting and listing passes, the JSON line pass,
  diagnostic judgment (`_judge`), failure texts (`explain_failure`) and the regex
  retry (`pattern_retry`).
- `core/tools/_search_results.py`: ordering, pages and rendering (`entry_page`,
  `content_window`, `content_page`) and the `summary`.

Two shared modules help. `core/tools/search.py` provides `SearchBudget`,
`MAX_OUTPUT_BYTES` and path display; its old walker remains only for Chat file
mentions, which have their own UI discovery contract. `core/tools/_path_suggestions.py`
(shared with `read`) suggests similar paths. The async handler runs the whole
operation through `run_tool_worker`.

## Call Interpretation

No field is required. The named fields cover the common calls: `pattern` (one
regex), `path` and `glob` (a string or a list), `output` (`content`, `files`,
`count`) and `context`. `limit` and `offset` page through results. `args` adds
ripgrep arguments, one per item, without an executable name, shell quoting or
shell expansion. On its own, `args` is a complete ripgrep argument list. `pattern`
acts like `-e`, `path` entries are roots and `glob` entries are `-g` filters.

Rules for `args`:

- Without `pattern`, `-e` or `-f`, the first operand is the pattern and later
  operands are roots. Options can come before or after operands, and `--` ends
  option parsing.
- Short clusters split. A valued short flag accepts `-m5`, `-m 5` and `-m=5`.
  A value attached to a valueless flag (`-F=true`) is rejected with a correction.
- Most flags pass through to ripgrep unchanged, so ripgrep's own semantics and
  later-flag-wins precedence hold.
- `_search_query.py` owns these flags:
  - **Output-shape flags** (`-l`, `-c`, `--count-matches`, `--files-without-match`,
    `-q`) select the mode.
  - **`--files` and `--dirs`** select listings; they exclude each other.
  - **`-o`, context, sort, `--limit` and `--offset`** are handled by vBot.
  - **Silent display flags** (`-n`, `--heading`, `--color`, `--json`, ...) are
    dropped because the fixed output already satisfies them.
  - **`-N`, `-I`/`--no-filename`, `--passthru` and `--replace`** are ignored
    with a `note`; the `--replace` note names the offered file edit Tool
    (`offered_edit_tool`: `apply_patch` or `edit`, `tools/edit.md`), or none
    when neither is offered. grep's `-h` is also ignored with a note when the
    call has something to search; on its own, `-h` shows help.
  - **grep spellings**: `--include`, `--exclude` and `--exclude-dir` become globs.
    `-r` and `-R` are grep's recursive switch. `-E` followed by something that is
    not an encoding is grep's extended-regex flag.
  - **Blocked flags** (`--pre`, `--pre-glob`, `--hostname-bin`, `--generate`) would
    run other programs; they are rejected.
  - **`--help`, `--type-list`, `--version` and `--pcre2-version`** return
    references instead of searching.
- ripgrep rejects unknown flags itself. `explain_failure` rewrites that rejection
  as a correction.

Modes:

- With no pattern, the call lists files. `{}` lists the working directory, and
  `glob` or `path` narrow it.
- A pattern beside `--files` is rejected with the field that was sent.
- A pattern beside `--dirs` filters directory names, with a `note`
  (`name_patterns`). ripgrep matches them (`match_names`): the names go
  NUL-separated through `--null-data` with the query's matching flags (`-i`,
  `-S`, `-F`, `-w`, `-x`, `-v`, `-P`, `--engine`, ...), so the `pattern`
  description's "ripgrep syntax" holds and `-x` compares the whole name. A
  rejected pattern takes the content search's repairs (`pattern_retry`) and
  otherwise its error, even when there are no directories. Agents sent `--dirs`
  with `pattern: "^empty$"` (first-use probe, 2026-10).
- `output: "count"` or context without a pattern is rejected.
- Context with count or file-list output is dropped with a note (Sessions,
  2026-09: Agents sent `output: "files"` with `context: 3`).
- Output flags beside a listing are ignored with a note.
- A glob-shaped pattern lists files matching it, with a `note`, when no
  conflicting option is set. This applies only to content or files output with a
  single, non-literal pattern. Glob-shaped means: no whitespace or `()|^$+\`,
  plus a leading `*`, `**` or `/*.`. A pattern that matches every name (`*`,
  `**/*`, ...) lists all files.

The owner normalizer repairs common encodings and wrappers. It also accepts field
names from other search Tools and command-line habits when their meaning is exact
(`_FIELD_ALIASES`, `_translate_flag_fields`):

- **Renamed fields:** `query`, `include`, `output_mode`, `head_limit`, `argv`, and
  the old `options`, `patterns` and `paths`.
- **Flag fields:** `-i`, `ignore_case`, `literal`, `-C`, `type`, `exclude`,
  `recursive: false` (becomes `-d 1`), Hermes-style `target: "files"`, and others.

Other rules:

- Single-letter keys keep ripgrep's case: `-c` counts, `-C` is context.
- A `pattern` list becomes repeated `-e`.
- Unclear values, conflicting aliases and unknown fields are rejected.
- The old `action` and `kind` fields are unknown fields.
- A double-quoted encoded list in `args` keeps regex backslashes that are missing
  from its JSON escaping.
- A scalar `args` string that starts with an option and contains no backslash
  splits like a command line. One with backslashes asks for one argument per item
  and shows the split list as an example.
- A `path` or `glob` string that is a JSON list of strings is that list.

Roots (`_resolve_roots`):

- Without roots, the call searches the effective cwd. An empty path or `-` is
  rejected.
- Relative roots start at the cwd; absolute roots are allowed.
- If a missing root is a brace or comma list whose parts all exist, each part is
  searched, with a note.
- If a missing root contains glob characters and no other glob is set, the call
  searches the root's fixed directory with the rest as an anchored glob, with a
  note.
- Any other missing root is reported, never replaced. Each one gets up to five
  suggestions, and the remaining roots are still searched; the result carries
  warnings and `searched_paths`. A relative root without suggestions names the
  working directory it was resolved against, since Agents pass paths relative to
  another directory (Sessions, 2026-09: 48 `path_not_found` failures, mostly
  relative roots). When every root is missing, the call fails with `path_not_found`, ending
  `Nothing was searched. Correct path, or omit it to search the working directory.`
- When the pattern came from the first `args` operand, the warning shows how to
  search missing words as further patterns. Words are missing operands without a
  path separator. The suggestion takes the form `pattern "a|b|c"`, or repeated
  `-e` with `-F`.
- When `--files` was given operands, the warning points to `-l` for listing files
  by contents.
- Real calls showed this shape (Sessions, 2026-09 and 2026-10): several search
  words passed as operands, and a pattern passed beside `--files`.

## Engine Contract

Every ripgrep run gets `DEFAULT_ARGUMENTS`
(`--no-config --hidden --no-require-git --glob-case-insensitive`), then the
query's rg arguments, so later `args` override them (`--no-hidden`,
`--no-glob-case-insensitive`). `--glob=!.git` always comes last. This gives the
following selection:

- hidden files are included;
- `.gitignore`, `.ignore`, `.rgignore`, `.git/info/exclude` and the global Git
  excludes apply, also outside Git repositories;
- binary files are skipped;
- `.git` is never searched.

A positive glob overrides ignore rules, as in ripgrep. File types match names in
any letter case while globs do (the default): ripgrep compares type globs
case-sensitively, so `widen_type_case` reads `--type-list` (honoring the query's
`--type-add`/`--type-clear`) and adds each selected type's globs again with every
letter as `[xX]`; `-t py` then finds `b.PY` (first-use probe, 2026-10).
`--no-glob-case-insensitive` keeps ripgrep's case-sensitive types. Globs without `/` match
names at any depth. A glob with `/` is anchored at the cwd scope and also at each
searched directory root (`rg_globs`), and `./` anchors like `/`.

Scopes (`_scopes`): roots inside the cwd share one run at the cwd, and each
outside root gets its own run, so labels stay cwd-relative or absolute.

A content search runs in two phases:

1. A counting pass runs over all roots:
   - normally `--count --with-filename --null --stats`;
   - `--count-matches` with `-U`, `-o` or `--count-matches`;
   - `--files-without-match --null` for that mode.
2. For line output, a `--json --max-count N` pass runs over only the page's files,
   given as explicit paths in batches of at most 28,000 command-line bytes.

Listings use `--files --null`. ripgrep lists no directories, so `--dirs` runs
`--files --debug` with only the excluding globs and collects the paths ripgrep's
walker reports as skipped (`ignoring <path>: Ignore(...)`, captured by
`native_lines`; file type lines are dropped because types skip files only). The
owner then walks the directories without those paths (`_walk_directories`), so
empty directories are listed and ignore rules, hidden paths and excluding globs
stay ripgrep's decisions. Links and junctions are entered only with `--follow`.
`--max-depth` and the selecting globs apply to the directories themselves
(`_directory_selected`, with `PurePath.full_match`). With `-t` or `-T`, only
directories holding a selected file are listed. The `--debug` line format is
ripgrep 15.1.0's; `test_directory_lists_include_empty_directories_ripgrep_enters`
fails if an upgrade changes it.

Result units:

- Without `-U`, one result is one matching line, which is consistent with `-c`.
- With `-U`, one result is one match. Rendering splits ripgrep's merged JSON
  events by submatch line spans; a line shared by several matches appears once
  per page.
- With `-o`, one result is one match.

Documented deviations from plain `rg`, all deliberate:

- Content is ordered by path, comparing casefolded components, across all
  roots. Listings are ordered newest first.
- `-uuu` shows binary matching lines instead of the "binary file matches"
  message.
- With `-U`, results and counts count matches.
- `--dirs` exists; it lists every directory ripgrep's walker enters, empty ones
  included.
- `-q` returns the file list.
- `-o` shows no column.

Diagnostics (`_judge`):

- IO errors (`(os error N)`) become English warnings that name the path, with
  the reason from `file_state.os_error_reason`.
- Link loops become warnings ("Skipped a link loop").
- Any other diagnostic that starts with an existing path becomes a warning, for
  example an invalid ignore file.
- Anything else means ripgrep refused the query, and the call fails with
  `search_error` and a correction. Exit 1 means no match.
- If ripgrep rejects a regex and the call is not literal, `pattern_retry` runs
  once:
  - look-around or backreferences rerun with PCRE2;
  - unbalanced parentheses, braces and dangling repetition operators are escaped;
  - unnecessary escapes before Unicode punctuation are removed;
  - the `note` names the change and `-F`.

  When nothing applies, the call fails with the native message and the `-F`
  correction.
- Windows junctions count as links, as in ripgrep's walker
  (`test_junctions_are_followed_only_on_request_or_as_explicit_roots`).

## Results and Paging

A successful result has these fields:

- `data.summary`: totals, the shown range and how to continue.
- `content`: the result lines.
- `next_offset`: set when more results exist.
- `note`: present when interpretation changed the call.
- `warnings`: at most 20.
- `searched_paths`: present when a root is missing or nothing was found.
- `patterns`: present when nothing was found; it lists the actual patterns.
- `skipped`: present when nothing was found or the call lists entries, and ignore
  files (`.gitignore`, `.ignore`, `.rgignore`) excluded paths in the walk. It names
  the five shallowest, counts the rest and points to `-u`. ripgrep's `--debug`
  walk lines of kind `Gitignore` supply them (`NativeOutcome.ignored`); hidden-file,
  glob and type exclusions are what the call asked for and are not named. Agents
  checked correct empty and listed results with shell listings such as
  `Get-ChildItem -Recurse -Force` (first-use probe, 2026-10: every `search_dirs`
  failure and most deepseek-v4.1-flash failures); results with matches leave it
  out to keep history small
  (`test_results_name_paths_ignore_files_excluded`).

Content lines:

- Matching lines are `path:line:text` and context lines are `path-line-text`.
  Gaps between non-adjacent lines are marked with `--` when context is shown.
- Paths are relative to the cwd when possible and absolute otherwise.
  Directory entries end in `/`.
- Names with control characters are JSON-quoted so they can be passed back.
- Line numbers equal `read`'s, except in files with lone-CR line endings, which
  ripgrep does not count.
- Lines longer than 1000 characters become excerpts around the match, with
  `[N characters omitted]` markers.

Paging:

- `limit` defaults to 100 (maximum 10,000); `offset` defaults to 0 (maximum
  1,000,000). `--limit` and `--offset` in `args` work too; conflicting values
  are rejected with both values and `Pass <name> once.`
- Results define pages; context does not use result slots.
- A page ends at `limit`, or at the 50 KiB output budget minus reserved room,
  never inside a result.
- A page's first result always appears, if necessary without its context or
  trimmed.
- Context never shows a match that the page leaves out.
- Continuation repeats a live query, so file edits between pages or between the
  two phases can shift page boundaries.
- No matches is a success; its summary says how many files were searched.

The display (`_display_details`) shows the user the `results` text, an info
notice about the next page and each warning
(`test_the_user_sees_the_results_further_pages_and_warnings`).

## Bounds, Cancellation and Errors

- **Budget:** the shared 30-second `SearchBudget` covers all phases. A timeout or
  a Run cancellation returns partial results with a warning. A user cancellation
  kills the child and returns `cancelled_by_user`.
- **Child process:** each child's RSS is bounded at 512 MiB, polled every 50 ms.
  One protocol record is bounded at 8 MiB, the output queue and stderr are
  bounded, and one counting or listing pass is bounded at 256 MiB of output.
- **Entries:** at most 500,000 entries are collected; more makes the result
  incomplete, with a warning.
- **Process ownership:** native subprocess creation, termination and release stay
  in the worker thread. The cancel callback keeps only an event, never the
  `Popen`: dropping it on the Event Loop must not run a blocking Windows handle
  destructor (`test_search_files_lifecycle.py`).
- **OS errors:** an OS error that prevents the search fails with
  `search_error`, as `search_files could not run the search: <reason>. Retry the
  call.`, with an English reason and no scratch path. Sessions in 2026-09 showed
  Windows' German text naming a scratch file.

## Native Dependency and Permissions

`resources/ripgrep.lock.json` pins ripgrep 15.1.0 with PCRE2. It holds a
SHA-256 digest for each platform's archive and executable.

- **Locating the binary:** `core/utils/search_binary.py` resolves only private
  assets under `resources/native/ripgrep/`. There is no PATH search and no Python
  regex fallback.
- **Provisioning:** `cli/search_runtime.py` provisions at install, update and
  build time. Downloads and extraction are bounded, and each asset is checked for
  integrity, validated as an executable and replaced atomically. Provisioning
  uses only the Python standard library.
- **Missing or corrupt assets:** the Tool is not ready and shows a repair hint. A
  verified existing asset works offline, and invoking the Tool never provisions.
- **Packaging:** server packaging includes the executable and its notices; the
  desktop client excludes them.

Only `search_files` is registered. New Project ceilings include it. A `grep` or
`glob` name left in a policy is an ordinary unknown Tool name. Claude/OpenCode
scanner denials for either capability map to `search_files`. Historical chat rows
remain readable.

## Agent-facing text

| Text | Reason |
|---|---|
| `Search file contents with a regular expression, or list files and directories.` | Names every job of the single Tool so Agents pick it for name and content discovery (F1). Without "directories", Agents asked for directories went straight to the shell (first-use probe, 2026-10). |
| `Use this instead of grep, rg, find, or ls in the shell.` | Agents otherwise fall back to the shell for search (F1). |
| `Find text: {...}. List files: {...}. List directories: {...}.` | Complete canonical first calls; weak Models copy examples (F2). The directory example shows `--dirs`, which Agents otherwise did not find (first-use probe, 2026-10). |
| `Matches come back as path:line:text; file lists are newest first.` | Agents need the output shape and order to read results without rereading files (F4). |
| `Hidden files are included, .gitignore rules apply, and .git is skipped.` | Explains why ignored files are absent, so a missing hit is not read as absence (F4). |
| `Results come in pages; continue with next_offset.` | Prevents reading page 1 as everything (F4). |
| pattern: `Regular expression (ripgrep syntax) ... Omit to list files.` | Pins the regex flavor and the listing default (F2, F3). |
| pattern: `To match text containing ( [ . * literally, add "-F" to args.` | Code searches such as `foo(` were the most common regex failure (Sessions, 2026-09); the retry repairs unbalanced cases, but `.` and `*` still match as regex. |
| path: `File or directory ..., or a list of them. Relative paths start at the working directory. Omit to search the working directory.` | Weak Models filled `path` with guesses; states the base of relative paths (F2, F3). |
| glob: `File name filter, or directory name filter with --dirs, such as *.py ...; a leading ! excludes. Without a / it matches names at any depth. Case-insensitive. A list applies each.` | Glob anchoring and case differ between harnesses (F3). "directory name filter with --dirs" keeps the text true for the description's `List directories` example (F3). |
| glob: `Omit to include every name.` | Omit rule (F2); glob is set in 530 of 4774 Session calls, the most frequent values real filters, so the sentence guards weak Models rather than an observed failure (Sessions, 2026-09). |
| output: `content ...; files ...; count ...` | Names the three shapes in Agent terms (F2). |
| output: `..., or every match with "--count-matches" in args.` | Agents asked for occurrences per file knew `count` counts lines but did not find `--count-matches`; they read files or tried `rg -o` in the shell (first-use probe, 2026-10). |
| output: `Omit for content.` | Omit rule in the shared form (F2); replaces "(default)". Agents sent `output: "content"` in 1769 calls, harmless but needless (Sessions, 2026-09). |
| context: `Lines to show before and after each match.` | Unit and meaning (F2). |
| context: `Omit to show only the matching lines.` | Agents set context in 2427 of 4774 calls, mostly 2-6 lines, which multiplies result size (F6, Sessions, 2026-09). |
| args: `More ripgrep arguments, one per item: -i ..., --dirs list directories.` | One item per argument prevents command-line strings; the flag list covers the common needs without opening help (F2, F6). |
| args: `A plain ripgrep argument list also works: the first operand is the pattern, later ones are paths.` | Agents write rg argument lists from habit (862 calls used args, Sessions 2026-09). |
| args: `["--help"] lists every option.` | Route to the full flag reference instead of guessing (F5). |
| limit: `Maximum results per page. Omit for 100.` | Plannable number (F6). |
| offset: `Results to skip; pass next_offset to get the next page.` | Continuation uses the returned value, never a computed one (F4). |
| offset: `Omit to start at the first result.` | Omit rule (F2); Agents sent `offset: 0` in 213 of 217 calls that set it (Sessions, 2026-09). |

The help text (`help_text`, `HELP_EXAMPLES`) and result and error texts are
covered by `test_search_files_arguments.py` (the help examples run against a
fixture) and the error parametrizations there.

## Verification

Tests live in `tests/core/tools/test_search_files*.py`:

- **`test_search_files.py`** checks ripgrep equivalence and engine behavior:
  - content output and paging are compared with `rg --sort=path` over a fixture
    with every ignore source;
  - file lists are compared with `rg --files`;
  - a git differential (`git ls-files --others --exclude-standard`) checks ignore
    selection;
  - other tests cover totals, ordering, context at page edges, the byte limit,
    multiline paging, outside roots, `.git`, excerpts, encodings, link loops,
    junctions, unusual names, timeout and cancel, English OS errors, the missing
    engine, and the display.
- **`test_search_files_arguments.py`** checks interpretation and tolerance:
  named fields, aliases, grep habits, command-line strings, path globs and lists,
  missing paths and their hints, flags owned by vBot, conflicts, regex repair, and
  the help examples.
- **`test_search_files_lifecycle.py`** checks native child lifecycle, memory
  polling and cancellation with real subprocesses.

Other tests: `tests/cli/test_search_runtime.py` covers provisioning. The probe
cases in `scripts/provider_probe/workflow_search_files.py` run through
`tests/scripts/test_provider_probe.py`. Runtime, scanner, Chat, packaging and Tool
row integration tests also cover the Tool. Tests execute the private native
engine.

Live probes cost money; ask before running them:

- `scripts/probe_provider_tool_call.py --scenario tool_first_use --first-use-tool search_files`
  evaluates natural user tasks with production definitions, competing Tools and
  real files.
- `--scenario search_files` (with `--search-case`) is guided single-Tool
  conformance; it cannot establish Tool choice.

To check real call shapes, use `python -m scripts.tool_lab sessions <data-root>
--tool search_files`. It reads a copy of `sessions.db`.
