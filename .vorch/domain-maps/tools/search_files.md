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

- `core/tools/search_files.py`: registration, scopes, orchestration (`_search`),
  the handler and its bound failure texts (`_bound_failure`), and the display.
  It re-exports the definition and call-reading entry points used by callers.
- `core/tools/_search_arguments.py`: the owner normalizer
  (`normalize_search_arguments`) and `interpret_search_call` (the provider probes
  use it too), translating call dialects into the named fields and `args`.
- `core/tools/_search_roots.py`: root resolution, path/glob interpretation and
  missing-path diagnostics.
- `core/tools/_search_confinement.py`: Permission Rules for a restricted Run
  (Permission Rules, below).
- `core/tools/_search_definition.py`: the Tool schema, description and help
  reference shared by registration and call reading.
- `core/tools/_search_query.py`: `interpret` turns the named fields and `args`
  into one `SearchQuery`, with mode, patterns, roots, globs, the rg arguments,
  context, page and notes.
- `core/tools/_search_execution.py`: `SearchExecution` binds one native binary,
  Tool context, budget and working directory for the counting, listing and JSON
  line passes, name matching and native reference reads. It also owns diagnostic
  judgment (`_judge`), failure texts (`explain_failure`) and regex retry
  (`pattern_retry`).
- `core/tools/_search_native.py`: bounded, cancellable ripgrep children
  (`native_lines`), pipe readers, process lifetime and native resource bounds;
  context-free picker listings use this same reader.
- `core/tools/_search_results.py`: ordering, pages and rendering (`entry_page`,
  `content_window`, `content_page`) and the `summary`.
- `core/tools/_search_hints.py`: when a pattern looks like file names
  (`file_name_extensions`), literal text, fixed text to count or a phrase that
  wrapped text can break across lines (`wrapped_phrases`), and the notes
  for results that miss what the pattern seems to ask for; `_search` runs the
  passes behind them.

Two shared modules help. `core/tools/search.py` provides `SearchBudget`,
`MAX_OUTPUT_BYTES` and path display, plus the listings the Chat `@` file picker
runs outside a Tool call: `list_selected_files` (the files a `files` listing
without arguments returns, read NUL-separated so a name with a line break stays
whole) and `unselected_names` (which entries of one directory that selection
leaves out, from ripgrep's `--debug` skip reports). They call
`native_lines` without a `ToolContext`, so only their budget stops the child, and
keep the picker on the same selection as the Tool, `__pycache__` exclusion
included (the picker marks cache directories as ignored). `core/tools/_path_suggestions.py`
(shared with `read`) suggests similar paths. The async handler runs the whole
operation through `run_tool_worker`.

## Call Interpretation

No field is required. The named fields cover the common calls: `pattern` (one
regex), `path` and `glob` (string lists in the schema; a single string is
wrapped), `output` (`content`, `files`, `count`) and `context`. Blank values
(`""`, or a list of only blank items) count as omitted. `limit` and `offset`
page through results. `args` adds ripgrep arguments, one per item, without an
executable name, shell quoting or shell expansion. On its own, `args` is a
complete ripgrep argument list. `pattern` acts like `-e`, `path` entries are
roots and `glob` entries are `-g` filters.

Rules for `args`:

- Without `pattern`, `-e` or `-f`, the first operand is the pattern and later
  operands are roots. Options can come before or after operands, and `--` ends
  option parsing.
- Short clusters split. A valued short flag accepts `-m5`, `-m 5` and `-m=5`.
  A value attached to a valueless flag (`-F=true`) is rejected with a correction.
- A valued flag that ends `args` without its value (`--glob`, `-g`, `--iglob`,
  `--include`, `-e`/`--regexp`, `-C`/`--context`, `--limit`, `--offset`) is
  left out with a `note` when the named field that gives such a value is set
  (`glob`, `pattern`, `context`, `limit`, `offset`): `--glob in args had no
  value and was ignored; glob gives "tools/*.py".` Without that field it is
  still rejected (`--glob in args needs a value as the next item, ...`), since
  the value is unknown. A Swarm Agent sent `args: ["--glob"]` beside
  `glob: "tools/*.py"` (Swarm run 2026-10-12, one call). Coverage:
  `test_a_flag_without_its_value_beside_the_field_that_gives_it_is_ignored`.
- Most flags pass through to ripgrep unchanged, so ripgrep's own semantics and
  later-flag-wins precedence hold.
- `_search_query.py` owns these flags:
  - **Output-shape flags** (`-l`, `-c`, `--count-matches`, `--files-without-match`,
    `-q`) select the mode.
  - **`--files` and `--dirs`** select listings; they exclude each other.
  - **`-o`, context, sort, `--limit` and `--offset`** are handled by vBot.
    `--head N` is read as `limit=N`, with a note that the count is results per
    page; it uses the same bounds and refuses a conflicting `limit`. This accepts
    a first-page bound sent as a familiar flag (Swarm run 2026-10-13).
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
- A pattern beside `--files` or `--dirs` filters the listed names, with a
  `note` (`name_patterns`, `_match_listed_names`): `--files` matches each file's
  name and shown path, as the name fallback below does; `--dirs` matches
  directory names. ripgrep matches them (`match_names`): the names go
  NUL-separated through `--null-data` with the query's matching flags (`-i`,
  `-S`, `-F`, `-w`, `-x`, `-v`, `-P`, `--engine`, ...), so the `pattern`
  description's "ripgrep syntax" holds and `-x` compares the whole name. A
  rejected pattern takes the content search's repairs (`pattern_retry`) and
  otherwise its error, even when there are no entries. A single non-literal
  pattern that matches every name (a glob such as `*` or `**/*`, or a regex
  such as `.`, `.*` or `^(.*)$` without `-v`, `-x` or `-w`) lists everything
  without a note, since matching it would list the same; a file name glob
  (`*.ts`) selects as `glob`, with a note. Agents sent `--dirs` with
  `pattern: "^empty$"` (first-use probe, 2026-10) and `--files` with
  `pattern: "."` (Swarm Sessions, 2026-10), which was rejected until then.
- `output: "count"` or context without a pattern is rejected.
- Context with count or file-list output is dropped with a note (Sessions,
  2026-09: Agents sent `output: "files"` with `context: 3`).
- Output flags beside a listing are ignored with a note.
- A glob-shaped pattern lists files matching it, with a `note`, when no
  conflicting option is set. This applies only to content or files output with a
  single, non-literal pattern. Glob-shaped means: no whitespace or `()|^$+\`,
  plus a leading `*`, `**` or `/*.`. A pattern that matches every name (`*`,
  `**/*`, ...) lists all files.
- With file-list output (`output: "files"`, `-l`), a single non-literal regex
  that matches every line (`.`, `.*`, `^(.*)$`, `^`, ...) lists every file
  instead, with a `note`, unless `-v`, `-x` or `-w` narrows it: ripgrep would
  leave out empty and binary files and count lines nobody asked for. Swarm
  Agents sent `pattern: "."` with `output: "files"` to list files (Sessions,
  2026-10).
- In content output such a regex stays a content search without a note: it
  returns what was asked for (every line, paged) and the summary gives its scale
  (`Showing matching lines 1-100 of 1452 matching lines in 13 files.`). Content
  calls with `.`, `^` or `.*` in the Session database (11 by 2026-10-12) served
  mixed purposes, such as reading one file named in `path` or dumping a tree,
  so no single reinterpretation fits them.
- Any other pattern stays a content search, because lines can contain a file
  name too (an import of `app.py`). When a content, files or count search finds
  no match, the handler lists the searched files (same paths, globs and flags)
  and matches their names and shown paths against the patterns
  (`_names_matching`, ripgrep with the query's matching flags). When some match,
  the call answers as a file listing of them (`Found 1 file, newest first.`,
  paged like a listing) with the `note` `No file contents match pattern, so the
  files whose names match it were listed.` An incomplete search (timeout,
  stopped Run) keeps its empty result. This replaced a hint shown only for
  regexes ending in a file extension (`\.py$`): DeepSeek V4.1 Flash sent
  `pattern: "app.py"` with `output: files` to find a file in 6 of 6 eval runs
  and got only `No matches`; a note naming the matching files did not change
  its next call either (agent-eval 2026-10-10). Coverage:
  `test_search_files_arguments.py::test_a_content_search_without_matches_lists_files_whose_names_match`.
- When no name matches either, a phrase a line break hid gets its note (Wrapped
  phrases, below); otherwise a single pattern (without `-v`) gets one of three
  notes (`_note_no_match`):
  - **Literal text.** A regex holding an operator with which it can miss its own
    text (`?+()[]{}^$\`; `.`, `*` and `|` cannot) is counted once more with
    `--fixed-strings`. When the literal text occurs, the `note` says so and names
    `-F`: `As a regular expression, pattern matches nothing ("?" is an operator);
    as literal text it occurs in 1 file. Add -F to args to search for it
    literally.` The call keeps its empty result. A pattern already under `-F`,
    or one `pattern_retry` repaired, gets no note. A Swarm Agent searched a URL
    with `?ele=` in it, got `No matches` and went on as if the URL were absent
    (Swarm run 2026-10-12, one call).
  - **Across lines.** Otherwise a regex without `-U` that holds `\s` (also in
    `[\s\S]`) or the `s` flag (`(?s)`, `(?is:`), written to match a line break,
    is counted once more with `--multiline`. When it then matches, the `note`
    says so and names `-U`: `Within one line, pattern matches nothing; across
    lines it occurs in 1 file. Add -U to args to let a match span lines.` The
    call keeps its empty result. A literal `\n` needs no note: ripgrep's error
    already names `-U`. An Agent searched a JSON file with `[\s\S]` between
    two lines without `-U` and got only `No matches` (Sessions, 2026-10-13);
    that pattern allowed one character where the file has a line break and
    indentation, so it matches nothing with `-U` either and gets no note.
  - **File names.** Otherwise, in content or files output, a pattern shaped like
    file names (`file_name_extensions` in `_search_hints.py`) whose names the
    complete listing did not match gets `No file name matches pattern either. To
    list similar file names, pass glob "harvest_*.txt" without pattern.` It
    applies when an extension in the pattern is one a searched file has, or, for
    files output with alternatives, when some searched file's name starts as
    the pattern does (its fixed start up to the last `_`, `-` or `/`). The
    example glob is that start plus `*` and the extension, or `*.<extension>`
    when no file starts so. An identifier (`load_config`), alternatives in content
    output, or alternatives no file name starts like (`save_a|save_b`) get no
    note. A Swarm Agent searched for files that did not exist with
    `output: "files"` three times in a row (names with extensions, with a
    directory prefix, without extensions), each answered only `No matches in 812
    searched files.` (Swarm run 2026-10-12). Coverage:
    `test_a_regex_without_matches_names_the_literal_search_when_its_text_occurs`,
    `test_a_regex_without_matches_names_the_multiline_search_when_it_spans_lines`,
    `test_a_file_name_pattern_without_matches_says_no_name_matches_either`.
- A files search (`output: "files"`) that does find contents, with one pattern
  shaped like file names (`file_name_extensions`: words, `-`, `/`, dots, `\.`,
  `\b`, anchors, groups and alternation only, with either an extension or a `|`,
  so `verify.py`, `tools/corpus\.py`, `(verify|corpus|fuzz)`,
  `harvest_(3|22)\.txt`), also matches the searched files' names on its first
  page (`_note_named_files`). Named files give the `note` `The listed files
  contain text matching pattern; files whose names match it: <up to 5 labels>[,
  and N more (add --files to args to list them)].` Without any, the `note` `The
  listed files contain text matching pattern; no file name matches it.` appears
  only when an alternative ends in an extension some searched file has, so
  `config.get` or a glob-narrowed selection makes no claim. `-v` skips it.
  Swarm Agents sent `verify.py|corpus.py|fuzz.py` with `output: "files"` to find
  files that did not exist yet and read the files mentioning them as an answer
  (Sessions, 2026-10). Coverage:
  `test_a_file_list_search_for_file_names_names_the_files_with_those_names`.
- Count output counts matching lines, as ripgrep's `-c` does. When the first
  page of a complete count of fixed text (`finds_fixed_text`: no operator
  besides `.` and `|`, escaped punctuation and `\b` allowed, a word character in each
  alternative; or any pattern under `-F`) finds more matches than lines, the
  `note` gives the total (`_note_match_count`, one more counting pass with
  `--count-matches`): `The counts are matching lines, which hold 3 matches in
  all. Add --count-matches to args to count matches per file.` A shape such as
  `.` or `\w+`, `--count-matches`, `-U` (which counts matches already) and `-v`
  get no note. A Swarm Agent counted an id that occurred three times on one JSON
  line, got `1` and needed a second call with `--count-matches` (Swarm run
  2026-10-12). Coverage: `test_a_line_count_says_how_many_matches_the_lines_hold`.
- **Wrapped phrases** (`_note_wrapped_phrases`, `wrapped_phrases`): wrapped
  prose (HTML, Markdown) breaks a phrase across lines, where words separated by
  spaces in `pattern` never match. When every pattern is fixed text (any
  pattern under `-F`, or `finds_fixed_text`) and one has a space between two
  words, a content, files or count search without `-v` or `-x` that found
  nothing, or found matches on its first page, counts once more with
  `--multiline`: one regex per gap between words, each requiring a line break
  there, so the count holds exactly the occurrences a search within lines
  misses. When it finds some, the `note` gives their number and the call that
  finds every occurrence: the patterns joined with `|`, `\s+` between words
  (literal text escaped), and `-U` (`-F` replaced or removed). Without matches
  within lines: `Within one line, pattern matches nothing; with a line break
  between its words it occurs 1 time in 1 file. To find it, pass pattern
  "and\s+the\s+feed-in" and add -U to args.`; beside matches: `The results
  leave out 1 match in 1 file with a line break between words of pattern. To
  include it, pass pattern "feed-in\s+tariff" and add -U to args.` The pass gets
  half the remaining time on its own budget, so running out leaves the note out
  without making the result partial; on this repository (3,500 files) it took
  about as long as the counting pass, 5-50 ms. Swarm run 2026-10-20: Agents used
  multi-word searches to confirm text was gone, and 12 of 177 plain multi-word
  searches missed an occurrence wrapped across lines, one of them answering only
  `No matches` (11 had other matches). Replaying the Run's 192 searches with a
  space in `pattern` gave this note on 12, the slowest call taking 113 ms.
  Coverage: `test_a_phrase_search_counts_the_matches_a_line_break_hides`.

The owner normalizer repairs common encodings and wrappers. It also accepts field
names from other search Tools and command-line habits when their meaning is exact
(`_FIELD_ALIASES`, `_translate_flag_fields`):

- **Renamed fields:** `query`, `include`, `output_mode`, `head_limit`, `argv`, and
  the old `options`, `patterns` and `paths`.
- **Output value wrappers:** `output>content`, `output>files` and `output>count`
  silently select the named canonical output; another prefix, value or a chain
  of values stays invalid. Two Calls on 2026-10-19 were retried with only
  `output>content` changed to `content`.
- **Flag fields:** `-i`, `ignore_case`, `literal`, `-C`, `type`, `exclude`,
  `recursive: false` (becomes `-d 1`), Hermes-style `target: "files"`, and others.

Other rules:

- Single-letter keys keep ripgrep's case: `-c` counts, `-C` is context.
- A `pattern` list becomes repeated `-e`.
- Unclear values (`ignore_case: "maybe"`, a flag field without a usable value) and conflicting aliases are refused.
- Unknown fields follow the shared rule (`tools.md` -> Unknown fields): the old `action` field refuses the call, the old `kind` runs without effect and says so.
- A double-quoted encoded list in `args` keeps regex backslashes that are missing
  from its JSON escaping.
- A scalar `args` string that starts with an option and contains no backslash
  splits like a command line. One with backslashes asks for one argument per item
  and shows the split list as an example.
- A scalar `args` string that broken list syntax wraps (`--files"]`,
  `['--files']`, `[--dirs]`, `["-i", "-t", "ts"`) is read as its items
  (`flag_list`, silently) when every item is a ripgrep flag or the value of the
  valued flag before it, brackets, quotes and commas occur only around items,
  and no backslash occurs. A valid JSON list is decoded first. Anything else
  stays as it was: `[u]` remains a pattern operand, `["-e", "[ab]"` the
  malformed-list error, since a bracket next to a value could belong to a regex
  or glob class. Swarm Sessions (2026-10): `--files"]` failed as an unknown
  flag, and `[--dirs]` beside a pattern became the root glob `/[--dirs]` and
  answered "No matches in 0 searched files".
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
- A missing root that starts with `!`, such as `"!vendor/**"` in `args`, is
  applied as an excluding glob, with a note; when no other root remains, the
  working directory is searched. An existing path starting with `!` stays a
  path. Agents passed exclusions as operands (first-use probe, 2026-10).
- Any other missing root is reported, never replaced. Each one gets up to five
  suggestions, and the remaining roots are still searched; the result carries
  warnings and `searched_paths`. A relative root without suggestions names the
  working directory it was resolved against, since Agents pass paths relative to
  another directory (Sessions, 2026-09: 48 `path_not_found` failures, mostly
  relative roots). When every root is missing, the call fails with `path_not_found`, ending
  `Nothing was searched. Correct path, or omit it to search the working directory.`,
  or, when every missing root came from `args`, `Nothing was searched. Correct the
  path[s] in args, or remove it [them] to search the working directory.`, since
  `path` was not set.
- A missing root that is a bare Swarm Wiki page number in the working directory
  (`w2`, `w2.md`) names the `swarm_wiki` read call instead of the working directory
  when `swarm_wiki` is offered, carrying a single plain-text pattern as `query`:
  `w2 is not a file or directory. Swarm Wiki page w2 reads with swarm_wiki
  {"action": "read", "page_id": "w2", "query": "Neumaier"}.` A regular expression
  is left out, since the Wiki searches plain text (`wiki_page_call` in
  `core/tools/_swarm_references.py`; Swarm Sessions, 2026-10-12: three searches
  with `path: ["w2"]` or `["w3"]`).
- When the pattern came from the first `args` operand, the warning shows how to
  search missing words as further patterns. Words are missing operands without a
  path separator. The suggestion takes the form `pattern "a|b|c"`, or repeated
  `-e` with `-F`.
- When `pattern` (or `-e`) set the pattern, every `args` item without a leading
  `-` is a path, so `pattern: "class "` with `args: ["def ", "return Result"]`
  failed with two missing paths and no sign that `args` takes options (Swarm
  review 2026-10-12). For a content search, missing operands without a path
  separator and without a similar existing path get a warning that names them
  and the call that searches them as further patterns: the sent `args` with `-e`
  before each of them (`_operand_pattern_warning`; verified: `pattern` and `-e`
  items combine). They stay paths, by the rule above: a missing path can also be
  mistyped, and searching it as text instead would search other files for other
  text without the Agent asking for it.
- When `--files` was given operands, the warning points to `-l` for listing files
  by contents.
- Real calls showed this shape (Sessions, 2026-09 and 2026-10): several search
  words passed as operands, and a pattern passed beside `--files`.

## Engine Contract

Every ripgrep run gets `DEFAULT_ARGUMENTS`
(`--no-config --hidden --no-require-git --glob-case-insensitive`), then the
query's rg arguments, so later `args` override them (`--no-hidden`,
`--no-glob-case-insensitive`). `--glob=!.git` always comes last, preceded by
`--glob=!__pycache__` unless the call asks for the caches (`skips_caches`). This
gives the following selection:

- hidden files are included;
- `.gitignore`, `.ignore`, `.rgignore`, `.git/info/exclude` and the global Git
  excludes apply, also outside Git repositories;
- binary files are skipped;
- `.git` is never searched;
- `__pycache__` directories are skipped unless `args` holds `-u`/`--no-ignore` or
  a selecting glob names `__pycache__`, `.pyc` or `.pyo`. The exclusion is an
  override glob, so no later glob can select the files inside; a root inside a
  cache is still searched, as ripgrep searches explicit roots. Swarm review
  (2026-10): `__pycache__` entries were 29% of the lines of whole-tree listings,
  and Agents turned to shell listings instead.

A positive glob overrides ignore rules, as in ripgrep. File types match names in
any letter case while globs do (the default): ripgrep compares type globs
case-sensitively, so `widen_type_case` reads `--type-list` (honoring the query's
`--type-add`/`--type-clear`) and adds each selected type's globs again with every
letter as `[xX]`; `-t py` then finds `b.PY` (first-use probe, 2026-10).
`--no-glob-case-insensitive` keeps ripgrep's case-sensitive types. Globs without `/` match
names at any depth. A glob with `/` is anchored at the cwd scope and also at each
searched directory root (`rg_globs`), and `./` anchors like `/`.
ripgrep matches globs against files, so a glob that is exactly the name or path
of a directory below the cwd or a root (`minidb`, `src/minidb/`) matched
nothing; `_directory_globs` adds `**/minidb/**` (or `src/minidb/**`, `/x/**`
for an anchored one) right after it, with a `note`. Excluding globs, globs with
glob syntax and `--dirs` listings (which match directories already) stay as
sent. Swarm Agents passed directories as `glob` and got `No files found.`
(Sessions, 2026-10).

Scopes (`_scopes`): roots inside the cwd share one run at the cwd, and each
outside root gets its own run, so labels stay cwd-relative or absolute.

A content search runs in two phases:

1. A counting pass runs over all roots:
   - normally `--count --with-filename --null --stats`;
   - `--count-matches` with `-U`, `-o` or `--count-matches`;
   - `--files-without-match --null` for that mode.
2. For line output, a `--json --max-count N` pass runs over only the page's files,
   given as explicit paths in batches of at most 28,000 command-line bytes.

Neither phase runs with `--debug`: its skip reports name every file a glob or
file type leaves out, which made such searches three times slower (probe on this
repository, 2026-10: a content search with a glob or `-t py` took 300-360 ms
with it and 85-125 ms without). Only a result without entries names the paths
ignore files excluded (`skipped`, below); it takes them from the name listing
that follows a search without matches, otherwise from one more
`--files --debug` walk (`ignored_scan`).

Listings use `--files --null --debug`, which reports the paths ignore files
excluded in the same walk. With `-t` or `-T` they run without `--debug` and
`ignored_scan` walks again without the type filters: ripgrep applies types
after ignore files and never to directories, so that walk skips the same paths
without reporting every file of another type.

ripgrep lists no directories, so `--dirs` runs `--files --debug` with only the
excluding globs and collects the paths ripgrep's
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

ripgrep prints a skipped path unescaped, so a name with a line break continues
its report on a line without the `rg: ` prefix. `native_lines` matches each
debug message once the next `rg: ` line or the end of stderr arrives, so the
path stays whole for every reader of these reports: the `--dirs` skip set, the
picker's `ignored` mark and the `skipped` summary
(`test_skip_reports_keep_a_name_with_a_line_break_whole`, a captured ripgrep
14.1.0 sample that runs on every platform).

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

- ripgrep exits with 0 after a match, 1 without one, and 2 after any error,
  also when a file it could not read sits beside matches. It refuses a query
  (regex, flag, glob or type errors) before it searches, so a refused run
  prints nothing and names no path. Only such a run fails the call, with
  `search_error` and a correction (`explain_failure`); a message that maps to
  no known refusal keeps ripgrep's text and points to `--help`, and a failure
  without any message says so and asks for a retry.
- A run that searched never fails over its diagnostics. IO errors
  (`(os error N)`) are files ripgrep could not read: the result names the
  first five, sorted, with the reason from `file_state.os_error_reason`
  (`Could not read <path>: <reason>.`), and counts the rest together with the
  messages past the stderr bound (`Could not read 395 more paths.`;
  `_Found.read_failures`). A not-found error for a path that no longer exists
  is dropped: the file was deleted between ripgrep's walk and its read, so
  nothing is missing from the result. Swarm Sessions (2026-10) marked results
  incomplete for vanished `*.sqlite-journal` files.
- Link loops become warnings ("Skipped a link loop"), and any other diagnostic
  that starts with an existing path names it, for example an invalid ignore
  file. Anything else becomes `ripgrep reported: <message>.`
- Swarm run 2026-10-20: a files search over a Scratch Area root holding a
  browser profile in use failed with `search_error` whose whole message was a
  path cut off mid-name plus `Nothing was searched.`, and the Agent switched to
  a shell listing (one call). Its locked files (`os error 32`) had filled the
  former 8 KiB stderr bound, the last message was cut mid-line, and the cut
  message, read as a refusal, discarded the whole search. Coverage:
  `test_files_ripgrep_could_not_read_are_warnings_beside_the_results`,
  `test_diagnostics_are_kept_as_whole_messages_and_the_rest_counted`.
- If ripgrep rejects a regex and the call is not literal, `pattern_retry` runs
  once:
  - a rejected newline in a content search reruns with `--multiline` unless
    `--no-multiline` was explicit (also `multiline: false` or `-U: false`);
    the pattern and dotall setting stay unchanged, and name matching never
    enables multiline implicitly;
  - look-around or backreferences rerun with PCRE2;
  - unbalanced parentheses, braces and dangling repetition operators are escaped;
  - unnecessary escapes before Unicode punctuation are removed;
  - the `note` names the change; pattern repairs also name `-F`.

  When nothing applies, the call fails with the error kind, the offending
  pattern as sent with a caret under the offending part, and the `-F`
  correction (`explain_failure`, `_pattern_error`); an unrecognized escape is
  named (`unrecognized escape sequence "\c"`). ripgrep reports the error in its
  combined form `(?:p1)|(?:p2)` with the caret placed for it, which showed
  Agents a pattern they never sent (Swarm Sessions, 2026-10). Errors that do
  not map onto one pattern (PCRE2, a pattern with a line break, `-f` files)
  keep ripgrep's message.
- Windows junctions count as links, as in ripgrep's walker
  (`test_junctions_are_followed_only_on_request_or_as_explicit_roots`).

## Results and Paging

A successful result has these fields:

- `data.summary`: totals, the shown range and how to continue.
- `content`: the result lines; absent when there are none (the `result_schema`
  requires no field).
- `next_offset`: set when more results exist.
- `note`: present when interpretation changed the call.
- `warnings`: at most 20.
- `searched_paths`: present when a root is missing; it names the roots still
  searched, as labels like the result paths.
- `skipped`: present when nothing was found or the call lists entries, and ignore
  files (`.gitignore`, `.ignore`, `.rgignore`) excluded paths in the walk. It names
  the five shallowest, counts the rest and points to `-u`. ripgrep's `--debug`
  walk lines of kind `Gitignore` supply them (`NativeOutcome.ignored`, from the
  listing or `ignored_scan` walk described under Engine Contract); hidden-file,
  glob and type exclusions are what the call asked for and are not named. Agents
  checked correct empty and listed results with shell listings such as
  `Get-ChildItem -Recurse -Force` (first-use probe, 2026-10: every `search_dirs`
  failure and most deepseek-v4.1-flash failures); results with matches leave it
  out to keep history small
  (`test_results_name_paths_ignore_files_excluded`).

Content lines:

- Each file is named once, as in ripgrep's `--heading`: its path on a line of
  its own, then its matching lines as `line:text` and context lines as
  `line-text`; an empty line precedes the next file's path. `-o` matches use
  the same layout. Gaps between non-adjacent lines of one file are marked with
  `--` when context is shown. A page that starts inside a file names it again.
  The former `path:line:text` on every line made path prefixes 28.6% of all
  result characters in one Swarm Run, where 184 of 234 content results came
  from one file (Sessions, 2026-10-12); re-rendering its 233 content results in
  this layout shortened them by 27.5% (24.2% of all its search_files result
  characters). `test_content_results_equal_ripgreps_across_pages` compares the
  output with `rg --heading`.
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
  The summary says when context was left out. For a trimmed result it gives the
  result's line span and the line to read on from, since paging cannot reach the
  rest of one result.
- Context never shows a match that the page leaves out.
- Continuation repeats a live query, so file edits between pages or between the
  two phases can shift page boundaries.
- An `offset` at or past the last result shows the first page, with a `note`
  (`_restart_past_the_end`). Swarm Agents kept an offset after changing the
  query and got only `No results at offset 80; the search found 11 ...`
  (Sessions, 2026-10).
- No matches is a success; its summary says how many files were searched.
  When selecting globs left no file (no file searched, or an empty listing)
  and no type, depth, size or hidden-file flag narrowed the walk, the summary
  names them instead (`_unmatched_globs`): `No file matches the glob
  "verify.py".`, `No directory matches the globs "a", "b".` A listing that
  matches a pattern against names keeps `No files found.`
- Swarm review (2026-10) of 42 empty results: `searched_paths` held only the
  absolute working directory, in another path form than the content lines;
  `patterns` echoed the call; `content` was an empty string; and no result
  named the glob that filtered everything out (`glob: ["verify.py"]` beside
  `pattern: "verify.py"` answered `No matches in 0 searched files.`). Empty
  results now carry the summary, and `skipped` when ignore files excluded
  paths. The glob-shaped pattern notes repeated their advice on 19 calls and
  now state only the repair.

The display (`_display_details`) shows the user the `results` text, an info
notice about the next page and each warning
(`test_the_user_sees_the_results_further_pages_and_warnings`).

## Bounds, Cancellation and Errors

- **Budget:** the shared 30-second `SearchBudget` covers all phases. A timeout or
  a Run cancellation returns partial results with a warning. A user cancellation
  kills the child and returns `cancelled_by_user`.
- **Child process:** each child's RSS is bounded at 512 MiB (`MAX_CHILD_MEMORY`),
  polled every 50 ms. One output record is bounded at 8 MiB, the output queue is
  bounded, and one counting or listing pass is bounded at 256 MiB of output.
  stderr keeps whole messages up to 64 KiB (`_DIAGNOSTIC_BYTES`; a message is an
  `rg: ` line with its continuation lines); a message that no longer fits is
  left out and counted (`NativeOutcome.dropped`), so no reader sees a message
  cut short. `--debug` reports are matched as they arrive and are not kept. A record is a line, or one path or name where ripgrep ends them with
  NUL (`--files --null`, `--files-without-match --null`, `--null-data`, and the
  picker's `list_selected_files`); those callers pass `native_lines` the NUL
  `terminator`. Read as lines, a file list over 8 MiB was one record and failed
  the call (probe, 2026-10: 66,000 files; `--dirs` failed alike, since it reads a
  file list first).
- **Bound failures:** a record over 8 MiB or a child over 512 MiB raises
  `SearchBoundError` (`bound` `record` or `memory`), and the handler returns
  `search_error` from `_bound_failure`, worded for what the call did, never the
  generic `tool_execution_error` (which claimed unknown effects and advised a
  file/count mode for listings). Only the line pass prints records that large, a
  match or context event of a very long line (or of a long match with `-U`), so
  `line_events` attaches the file from its `begin` event; the error names it,
  suggests the excluding glob (cwd-relative path, or the bare name outside the
  cwd) and `output` `files` or `count`. The memory error advises narrowing
  `path`/`glob` and `--max-filesize` for content searches, also leaving out `-U`
  when set, and narrowing alone for listings
  (`test_a_result_over_the_record_bound_fails_naming_the_file_to_leave_out`,
  `test_a_search_over_the_memory_bound_fails_with_advice_for_its_mode`).
- **Entries:** at most 500,000 entries are collected per pass; more makes the
  result incomplete, with a warning naming the count. `_collect` counts the
  records holding NUL as entries (every pass prints its entries with `--null`;
  the `--stats` lines hold none) and stops reading at the entry after the bound,
  which closes `native_lines` and so ends the child instead of reading on to
  256 MiB. When the 256 MiB output bound stops a pass first (paths averaging over
  about 537 bytes), the warning names the files kept and that reason
  (`ScanResult.stopped_by`;
  `test_a_pass_a_bound_cut_short_names_its_reason_and_the_files_kept` also checks
  that at most one record past the kept ones is read).
- **Process ownership:** native subprocess creation, termination and release stay
  in the worker thread. The cancel callback keeps only an event, never the
  `Popen`: dropping it on the Event Loop must not run a blocking Windows handle
  destructor (`test_search_files_lifecycle.py`).
- **OS errors:** an OS error that prevents the search fails with
  `search_error`, as `search_files could not run the search: <reason>. Retry the
  call.`, with an English reason and no scratch path. Sessions in 2026-09 showed
  Windows' German text naming a scratch file.

## Permission Rules

A restricted Run searches only what its rules let it read; full access adds no
argument and no check.

- Every root, alternative, glob base and directory-glob candidate is checked
  before its status (`_search_roots.py`); a refused root fails
  `permission_denied` before anything runs, and suggestions name only readable
  paths.
- `--file` and `--ignore-file` values are checked and passed as absolute paths;
  `-L`/`--follow` is dropped with a note, because ripgrep follows no links
  then and the checked roots stay the boundary (`confine_flags`).
- Below a readable root, `refusable_below` patterns become anchored excluding
  globs after every other glob (`--glob=!/<scope path>/<pattern>`); results,
  counts, directory listings and diagnostics are filtered again by
  `check_path`, which also covers patterns that do not translate into globs.
- Open: ripgrep still reads ignore files of parent directories and the global
  gitignore; `files_searched` counts can include files the filter removes.

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

The definition is cut to what the first call needs (about 300 estimated
tokens, down from 558). Removed sentences moved to the moment the Agent needs them:
results state their format, page continuation (`next_offset`) and the paths
ignore rules skipped (`skipped`); tolerance absorbs unbalanced regex
characters, plain rg argument lists, single strings for list fields and
blank placeholders, so no sentence has to warn about them.

| Text | Reason |
|---|---|
| `Search file contents with a regular expression, or find files and directories by name.` | Names every job of the single Tool so Agents pick it for name and content discovery (F1). "find ... by name" matches how Agents phrase the task: with "list files and directories", 6 of 12 directory-name trials went to the shell first, with "find ... by name" 2 of 12 (first-use probe, 2026-10). |
| `Use this instead of grep, rg, find, or ls in the shell.` | Agents otherwise fall back to the shell for search (F1). Names only Unix commands Models know from training; shell-specific cmdlet names stay out. |
| pattern: `Regular expression (ripgrep syntax) for file contents. Omit to list files.` | Pins the regex flavor and the listing default (F2, F3). "for file contents" keeps name regexes such as `\.py$` out of `pattern`; a content search that finds nothing lists the files whose names match (first-use probe, 2026-10; agent-eval 2026-10-10). |
| path: `Files or directories to search. Omit for the working directory.` | Weak Models filled `path` with guesses (F2); a relative miss names the working directory in the error. |
| glob: `Case-insensitive name filters such as *.py; a leading ! excludes.` | Without "Case-insensitive", Agents asked to ignore extension case wrote `*.[pP][yY]`, `--iglob`, several globs or a shell command (first-use probe, 2026-10). Exclusion is the one other glob rule Agents need before the call; a `!` value passed as `path` is applied as an exclusion with a note (F3). |
| output: `files lists matching files; count counts matching lines per file, every match with "--count-matches" in args. Omit for matching lines.` | Names the shapes in Agent terms (F2). Agents asked for occurrences per file did not find `--count-matches` (first-use probe, 2026-10). Agents sent `output: "content"` in 1769 calls (Sessions, 2026-09). |
| context: `Lines around each match. Omit for none.` | Agents set context in 2427 of 4774 calls, which multiplies result size (F6, Sessions, 2026-09). |
| args: `More ripgrep arguments, one per item, such as -i, -w, -F, -t py, -u (include ignored files), --dirs (list directories, empty ones included).` | One item per argument prevents command-line strings; the examples cover case, word, literal, type, ignored files and directory listing, which Agents otherwise did not find (F2, first-use probe, 2026-10). "empty ones included" corrects the trained belief that ripgrep cannot list empty directories. |
| args: `["--help"] lists all.` | Route to the full flag reference instead of guessing (F5). |
| limit: `Results per page.` plus schema default 100 | Plannable number (F6). |
| offset: `Pass next_offset to continue.` | Continuation uses the returned value, never a computed one (F4); Agents sent `offset: 0` in 213 of 217 calls that set it (Sessions, 2026-09). |
| Help: `Output: content names each file on its own line, followed by line:text for its matching lines and line-text for context lines.` | States the heading layout (Results and Paging -> Content lines) for an Agent that asks how to read results (F4); it replaced `content shows path:line:text for matching lines and path-line-text for context lines` with the layout change (2026-10-12). |
| `Nothing was searched. Could not check <path>: <reason>. Check access to this path and retry.` | A failed root check is an unavailable path, not a missing one; it fails with `search_error` before any search, preventing false absence claims or advice to broaden the scope. |
| `path_not_found` for a page number: `w2 is not a file or directory. Swarm Wiki page w2 reads with swarm_wiki {...}.` | Three searches named a Wiki page number as `path` and got `Path not found` (Swarm Sessions, 2026-10-12). Only when `swarm_wiki` is offered. |
| Bare flag beside its field: `--glob in args had no value and was ignored; glob gives "tools/*.py".` | The call runs as the named field says; the note tells the Agent the flag had no effect, so it does not repeat it (Swarm run 2026-10-12: one call failed with `needs a value as the next item` although `glob` gave the value). |
| Across lines: `Within one line, pattern matches nothing; across lines it occurs in 1 file. Add -U to args to let a match span lines.` | A search whose regex was written to cross a line break (`[\s\S]`) found nothing without `-U`, and the empty result gave no cause (Sessions, 2026-10-13). Names the flag; only when the `-U` count finds the text, so the note never sends the Agent to a search that also comes back empty. |
| Wrapped phrase, no match within lines: `Within one line, pattern matches nothing; with a line break between its words it occurs 1 time in 1 file. To find it, pass pattern "and\s+the\s+feed-in" and add -U to args.` | Swarm run 2026-10-20: a phrase searched to confirm it was gone answered `No matches` while it occurred wrapped in HTML, which reads as a false "gone" (F4). `-U` alone does not help a literal space, so the note gives the pattern to send; `replace -F with -U in args` (or `remove -F from args`) when the call was literal, since the suggested pattern is a regex. Only when the count finds the text. |
| Wrapped phrase beside matches: `The results leave out 1 match in 1 file with a line break between words of pattern. To include it, pass pattern "feed-in\s+tariff" and add -U to args.` | 11 of the 12 plain multi-word searches that missed a wrapped occurrence in that Run had other matches, so a note only for empty results would have missed them (F4). "leave out" says the shown results are short of the whole; the suggested call finds every occurrence, those within lines included. |
| Rejected newline: `The pattern matches across lines, so the search ran with -U.` | Names the inferred multiline mode without claiming dotall or a changed pattern. A rejected `\n` search on 2026-10-19 led the Agent to abandon the relation between two lines and make two weaker searches instead (F2, F5). Explicit literal mode and `--no-multiline` keep their meaning. |
| Literal text: `As a regular expression, pattern matches nothing ("?" is an operator); as literal text it occurs in 1 file. Add -F to args to search for it literally.` | An empty result for a URL with `?` read as "the URL is absent" (Swarm run 2026-10-12). Names the operator so the Agent sees why, and the flag that searches the text. |
| File names: `No file name matches pattern either. To list similar file names, pass glob "harvest_*.txt" without pattern.` | `No matches` alone did not tell the Agent that names were checked too; it searched the same missing files three times (Swarm run 2026-10-12). "without pattern" keeps the suggested call a listing, since `glob` beside `pattern` still searches contents. |
| Count: `The counts are matching lines, which hold 3 matches in all. Add --count-matches to args to count matches per file.` | The Agent wanted occurrences and needed a second call (Swarm run 2026-10-12); the total answers most such calls without one. |
| Unclear flag value: `search_files was not run: "ignore_case" must be true or false; received "maybe".` (lists: `must be a value or a list of values`) | A flag field the call cannot read used to stay an unknown field; unknown fields can now run without effect (`tools.md` -> Unknown fields), which would search without the flag the Agent asked for (F3). |
| Repaired name globs: `Searched "..." as a repaired regular expression; no file contents or names match it. If you meant file names, omit pattern and pass glob [...].` | A file-name query expressed as glob alternatives was repaired as a content regex; the Agent read the empty result as proof that no such file existed (Swarm run 2026-10-13). Only after both content and name passes finish without matches, for plain glob-shaped alternatives; execution stays unchanged. It replaces the generic `-F` advice and keeps the repaired expression visible. |
| With existing globs: `To list files selected by the existing glob filters, omit pattern.` | Adding positive globs would widen ripgrep's selection, replacing them could lose exclusions. The correction retains those filters (including `--iglob`); no automatic change of search intent. A glob-like pattern with existing globs caused an empty content search in the same Swarm run. |
| `--head was read as limit=60 (results per page).` | The Agent sent `--head 60` to bound the results and got only unknown-flag advice (Swarm run 2026-10-13). Names the accepted interpretation and its counting unit (F2, F4). |
| Warning, and in `path_not_found`, for args items read as paths while `pattern` is set: `args takes ripgrep options, and an item without a leading - is a path, so "<item>" [and "<item>"] was [were] read as a path [paths]. To search it [them] as another pattern [further patterns], put -e before it [each]: args [<the sent args with "-e" before each such item>].` | An Agent passed further patterns as plain `args` items next to `pattern` and got only `Path not found: def`, which did not say that `args` takes options or how to pass more patterns (F2, F5; Swarm review 2026-10-12). The first sentence names the rule the Agent missed, the second the ready call, which keeps the other `args` items, since Agents send a suggested call as it stands. Not for an item with a path separator or a similar existing path, which reads as a mistyped path. |
| `path_not_found` ending when every missing root came from `args`: `Nothing was searched. Correct the path[s] in args, or remove it [them] to search the working directory.` | The former ending, `Correct path, or omit it ...`, named the `path` field, which such a call did not set (F5). |
| Warnings for unreadable files: `Could not read <path>: <reason>.` for the first five, then `Could not read 395 more paths.` | Swarm run 2026-10-20: a search beside a browser profile in use failed over its locked files and the Agent switched to a shell listing (one call; Diagnostics). The results stay; the names show where the unread files are, the count says how many without filling every warning slot with paths (F4, F6). |
| Warning for any other diagnostic of a search that ran: `ripgrep reported: <message>.` | Such a message used to fail the whole search, results included; as a warning it keeps them and marks the result incomplete (F4; Swarm run 2026-10-20, one call). |
| `-L (--follow) was ignored: under the permission rules, search_files does not follow links.` | The search ran without the flag the Agent sent; the note names the reason, so the Agent neither repeats it nor reads missing linked files as absent (F4). |
| Unknown refusal: `ripgrep refused the search:` / `<message>` / `Nothing was searched. Correct the call as that message says; {"args": ["--help"]} lists the accepted flags.` | Replaces `<message>` / `Nothing was searched.`, which gave no next step; in the Swarm run of 2026-10-20 its message was a cut path fragment, which read like nothing at all (F5). |
| Failure without a message: `The search engine failed without giving a reason. Nothing was searched. Retry the call.` | Replaces `search_files could not complete the search: the search engine exited with code 2. Retry the call.` / `Nothing was searched.`, which ran together two endings and named an exit code the Agent cannot act on (F5). |

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
    the output record bound per listed path and name, the bound failures and
    stop warnings, multiline paging, outside
    roots, `.git`, excerpts, encodings, link loops,
    junctions, unusual names, timeout and cancel, English OS errors, the missing
    engine, and the display.
- **`test_search_files_arguments.py`** checks interpretation and tolerance:
  named fields, aliases, grep habits, command-line strings, path globs and lists,
  missing paths and their hints, flags owned by vBot, conflicts, regex repair, the
  notes for results that miss what the pattern seems to ask for, and the help
  examples.
- **`test_search_files_lifecycle.py`** checks native child lifecycle, memory
  polling and cancellation with real subprocesses. The already-finished child
  case uses in-memory pipes and a fixed exit code, independent of process startup.

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
