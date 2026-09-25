# File Search Tool

Read when changing file/content search, search options, native engine provisioning,
search permission consolidation, or result continuation.

## Ownership and Interface

`core/tools/search_files.py` owns the single `search_files` Tool, registration,
normalization, orchestration, and display. Its private `_search_*` modules own
argument/option parsing, native execution, ignore rules, candidate selection, and
results; these are implementation units of the existing Tools owner, not public services.
The async handler offloads the complete operation through `run_tool_worker`.

The definition directs file/content discovery to this Tool instead of shell
rg/grep/find/ls. No field is required. Named fields cover the common calls:
`pattern` (one regex), `path` and `glob` (a string or a list), `output`
(`content`, `files`, `count`) and `context`; `limit` and `offset` page. `args`
adds ripgrep arguments, without an executable name, shell quoting, or shell
expansion, and alone remains a complete ripgrep argument vector.
`interpret_search_call` in `search_files.py` combines both into one query (the
provider probes use it too): `pattern` acts like `-e`, `path` entries are trailing
roots, `glob` entries are leading `-g` filters, and `output`/`context` become
`-l`/`-c`/`-C` for content searches. `_search_arguments.py` separates patterns,
options, and literal roots in `args`: without a named pattern or `-e`, the first
operand is the regex and later operands are roots; `-F` selects literal matching.
Repeated `-e`/`--regexp` patterns are ORed and make every operand a root. Options
may precede or follow operands; `--` ends option parsing. Option values remain
literal even when they resemble other flags.

A call without any pattern or operand lists files (`{}` lists the working
directory; `glob`/`path` narrow it). `--files`, `--dirs`, and `--entries` select
file, directory, and combined discovery explicitly; they are mutually exclusive,
all operands are roots, and `-g` filters names. `output: "count"` and `context`
without a pattern reject with the correction. Directory discovery includes empty
directories. No roots means `effective_cwd`; explicit empty roots and stdin reject.
Relative roots use that cwd, absolute roots are allowed, and symlink spelling stays
usable. `--help` and `--type-list` provide on-demand references.

Missing explicit roots produce partial results from the remaining requested roots,
with `missing_paths`, `searched_paths`, warnings and `complete=false`. If every root
is missing, the call fails with `path_not_found`. Each missing root names up to five
existing suggestions from `core/tools/_path_suggestions.py` (shared with `read`):
a relative path that repeats the end of the working directory, per-component
spelling repair, and similar sibling names. Suggestions are never applied, and roots
are never reinterpreted as patterns or replaced by the working directory. When the
pattern came from the first `args` operand, the diagnostic shows how to search the
missing operand as another pattern (`pattern "a|b"`, or repeated `-e` with `-F`).

The option catalog in `_search_options.py` owns aliases, arity, repeat/order
semantics, native forwarding, applicability, validation, and on-demand help.
This is a bounded search interface, never a shell command or unrestricted native
passthrough. The old `action`/`patterns`/`paths`/`options`/`kind` fields are no longer
part of the advertised interface (`options`, `patterns`, and `paths` are accepted
as aliases of `args`, `pattern`, and `path`);
historical persisted rows remain readable.
Supported families cover case/literal/word/line matching, PCRE2 and multiline,
contexts, file/count/absence/quiet output, globs and types, ignores, hidden paths,
symlinks, depth/size/filesystem limits, ordering, encoding, CRLF/NUL, binary
handling, and diagnostics. Unknown flags, missing operands, or incompatible
effects reject with a correction. Plain source coordinates and
formatting are fixed; formatting flags already satisfied by output are accepted.

Common scalar/container encodings and known call wrappers use shared repair.
The owner normalizer also accepts field names from other search Tools and
command-line habits when their meaning is exact (`search_files.py` ->
`_FIELD_ALIASES`, `_translate_flag_fields`): renames such as `query`/`include`/
`output_mode`/`head_limit`/`argv`, flag fields such as `-i`, `ignore_case`,
`literal`, `-C`, `-A`, `type`, `exclude`, `recursive: false` and Hermes-style
`target: "files"`, which become `args` tokens or named fields. Single-letter keys
keep ripgrep's case (`-c` counts, `-C` is context). A `pattern` list becomes
repeated `-e`. Unclear values (for example `ignore_case: "maybe"`), conflicting
aliases, and unknown fields reject. Inside `args`, grep spellings keep their
meaning: `-r`/`-R`/`-I` are accepted defaults, `--include`/`--exclude`/
`--exclude-dir` become `-g` filters, and `-E` followed by a non-encoding is grep's
extended-regex flag rather than ripgrep's encoding option. Arguments such as
`true`, `false`, `rg`, literal quotes, and shell syntax remain search payloads,
never executable prefixes or flag booleans. Complete content/listing examples in
the definition teach the canonical first call.

A glob-shaped pattern (no whitespace or `()|^$+\`, and a leading `*`, a `**`, or
`/*.`) with no `-F`, glob filter, context, count, or other content-only option lists
files matching it with a `note`; ripgrep rejects a leading `*` as a regex anyway.

Before shared scalar-array conversion, the owning `args` normalizer recognizes
double-quoted encoded lists. It preserves regex backslashes omitted from their
JSON escaping, such as field text `["-e","findMe\("]`. Broken list syntax or malformed
lists with ambiguous JSON control/unicode escapes require correction. Valid encoded JSON and
members of actual arrays retain their existing semantics, including literal quotes
and character classes. A scalar `args` string that starts with an option and has
no backslash splits like a command line, with quotes grouping words
(`"-F computeDamage( src"`); backslashes would be shell escapes there but regex
escapes here, so such a string stays one item and the parser asks for one argument
per item with the split list as example. Other scalar strings stay one pattern,
and quotes are never stripped from a scalar search pattern.

## Selection Contract

Content and path searches share traversal, union/deduplication, filters, and ignores.
Hidden paths are included by default. `.git` files and directories are always excluded.
Explicit ignored roots opt their subtree into searching. Other ignore sources,
from lower to higher precedence: global Git excludes and repository info/exclude,
applicable `.gitignore` files, `.ignore`, `.rgignore`, then extra ignore files.
Nearest repository boundaries include worktree pointer files and common excludes.
Controls can disable sources individually; unreadable rules never silently widen
the scope. Positive vBot filters narrow selection and cannot override ignores.
Compiled ignore files and Git `core.excludesFile` lookups are shared across calls
while the file's stat stamp (mtime, size, inode) is unchanged; files modified in the
last 3 seconds are always reread, so edits apply to the next call.

Name globs are case-insensitive unless explicitly changed. Bare `-g '*.py'` filters
basenames at any depth; globs containing `/` are root-relative, so `-g './*.py'`
selects top-level files. `-i`/`-s` control content case in content searches and glob
case in path discovery; explicit glob-case options also remain available.
Brace alternatives and character classes are supported. Ordered positive and
negative `-g`/`--iglob` filters apply to roots independently; negatives can exclude
directory descendants. File type/size filters on path searches require `--files`.
Overlapping roots deduplicate lexical paths; following symlinks remains opt-in
except an explicit root, preserves its spelling, and detects ancestor loops.
Windows junctions count as links (`is_link_entry`), like ripgrep's own walker
(`test_junctions_are_followed_only_on_request_or_as_explicit_roots`).

Default content ordering is path ascending; path discovery uses newest modification
first, with path tie-breaks. Explicit sorts cover path, modified, accessed, created,
and unsorted discovery; unsupported creation timestamps reject. A call-scoped
SQLite spool keeps union, deduplication, and sorting off unbounded Python lists;
it is discarded with the call, so it runs without fsync or an on-disk journal.

## Results and Resource Bounds

Success returns `data.content` and `complete`. Paths are relative to effective cwd
when possible, absolute otherwise; directory rows end in `/`. Content includes
source line numbers, and occurrence output adds byte columns. Line numbers equal
`read`'s (both ignore form feed, U+2028 and similar separators) except in files
with lone-CR line endings, which ripgrep does not count. No matches is success.
Quiet returns `matched=true/false`, or null when an incomplete scan proves neither.
`complete` is true only when the scan completed and no further result page remains.
A paginated result has `complete=false` and `next_offset`; an interrupted scan also
has `complete=false` with diagnostic warnings. When no result
was observed, `searched_paths` contains absolute resolved roots with forward slashes
and `patterns` contains the actual interpreted patterns, including literal quotes.
When investigating false negatives, check the effective cwd, literal quote or
escape characters in patterns, and subsequent narrowed searches before attributing
the outcome to the native engine.

`limit` defaults to 100 (maximum 10,000); `offset` defaults to zero (maximum
1,000,000). The equivalent `--limit`/`--offset` args options are accepted too;
duplicate field/flag values must agree, and option-value or post-`--` payloads
retain their literal meaning. These page controls never reach the native engine.
Logical matches or path/count rows define pages; context does not
consume match slots. Trailing context stops before a match that falls on the
next page, so a page never shows context around a hidden match. `next_offset` and a continuation instruction appear when
another result was observed. Continuation repeats a live query; filesystem edits
can change page boundaries. Long lines contain marked excerpts around the match;
context omission is explicit and never prevents continuation progress.

The shared 30-second SearchBudget polls traversal and native output, including
silent children. User cancellation kills the child and returns cancelled_by_user;
timeouts, Run cancellation, and unreadable entries make results incomplete with
bounded warnings. Regex errors fail even when selection is empty: the native content
run reports pattern and option errors itself, and a separate native check against an
empty file runs only when no candidate file was selected. Native exit 1
means no match; native diagnostics cannot become a successful empty search.
A rejected regex is retried once, only when nothing was observed and the intent is
evident (`_search_execution.py` -> `pattern_retry`): look-around or backreferences
rerun with PCRE2, and an unmatched parenthesis or a repetition operator with nothing
to repeat is escaped while the rest keeps its regex meaning. The result's `note`
names the change and `-F`. Anything else, such as an unclosed character class,
fails with the native diagnostic plus the `-F` correction. A child that exits before
process monitoring attaches still has its output, diagnostics and exit code drained
through the original process handle; memory monitoring remains active when available
and polls the child every 50 ms rather than per output record.

Independent bounds cover 50 KiB content output, 8 MiB native protocol records,
bounded pipe queues/stderr, 512 MiB child RSS, candidate storage (128 MiB), one
million observed entries, glob expansion, and process arguments. Failures and
exhausted bounds report actionable scope reductions. No persistent search handle
or candidate database survives the call. UI display shows the named fields and the
argument vector with a result-count fact; detail views retain warnings and
continuation metadata.

## Native Dependency and Permissions

`resources/ripgrep.lock.json` pins ripgrep 15.1.0 with PCRE2, per-platform archive
and executable SHA-256 digests. `core/utils/search_binary.py` resolves only private assets
under `resources/native/ripgrep/`; no PATH search or Python regex fallback exists.
`cli/search_runtime.py` provisions at install/update/build time with bounded
download/extraction, integrity checks, executable validation, and atomic replace.
Provisioning uses only the Python standard library so clean package builders can
download missing assets without installing application dependencies. The shared
locator lives in `core/utils/` to avoid importing the Tool registry in builders.
A verified existing asset works offline. Missing/corrupt assets make the Tool
not ready with an installation-repair hint; Tool invocation never provisions it.
Server packaging includes the executable and notices; desktop-client excludes it.

Only `search_files` is registered. New Project ceilings include it; startup never
migrates persisted selections, and a `grep`/`glob` name left in a policy is an
ordinary unknown Tool name without special meaning.
Claude/OpenCode scanner denials for either capability map to search_files.
The Generation 1 converter replaces grep/glob in every persisted Tool access list
(`scripts/converters/persistence_generation_1/_tool_access.py`): `search_files`
is granted only where both old capabilities were, a denial or a mode-all policy that
lost one capability denies it, and every replacement and narrowing is reported
(`database/generation-1-conversion.md` -> Retired Tool names). Historical grep/glob
chat rows remain readable.

## Verification

Primary tests: `tests/core/tools/test_search_files*.py` (encoded-list,
literal-payload, conflict, regex-repair, and empty-scope regressions in
`test_search_files_recovery.py`; named fields, other interfaces' spellings, grep
habits, command-line strings, and path suggestions in `test_search_files_fields.py`),
`tests/cli/test_search_runtime.py`, `tests/scripts/converters/persistence_generation_1/test_json_documents.py`,
`tests/scripts/test_probe_search_files.py`, plus runtime, scanner, Chat, packaging,
and Tool row integration tests. Tests execute the private native engine.

`scripts/probe_provider_tool_call.py --scenario tool_first_use --first-use-tool search_files`
evaluates natural user tasks with production definitions/prompts, competing Tools,
real disposable files and dispatch through the final answer. It checks discovery,
matching, counts, completeness, working-directory scope and appropriate read/shell
choices. `--repetitions` repeats fresh trials; `--first-use-report` retains every
attempt and response. `--scenario search_files` remains guided single-Tool
conformance, including options, repairs and rejection; it cannot establish Tool
choice or independent first-use reliability. Its `--search-case` selects case ids.
The old shared walker in `core/tools/search.py` remains for Chat file mentions;
its UI discovery contract is separate from search_files.
