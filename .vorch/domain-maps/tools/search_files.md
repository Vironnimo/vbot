# File Search Tool

Read when changing file/content search, search options, native engine provisioning,
search permission consolidation, or result continuation.

## Ownership and Interface

`core/tools/search_files.py` owns the single `search_files` Tool, registration,
normalization, orchestration, and display. Its private `_search_*` modules own
argument/option parsing, native execution, ignore rules, candidate selection, and
results; these are implementation units of the existing Tools owner, not public services.
The async handler offloads the complete operation through `run_tool_worker`.

Three advertised fields: required `args`, plus `limit` and `offset`. `args` is a
ripgrep argument vector, without an executable name, shell quoting, or shell
expansion. `_search_arguments.py` separates patterns, options, and literal roots:
content search uses the first operand as its regex and subsequent operands as
roots; `-F` selects literal matching. Repeated `-e`/`--regexp` patterns are ORed
and make every operand a root. Options may precede or follow operands; `--` ends
option parsing. Option values remain literal even when they resemble other flags.

`--files`, `--dirs`, and `--entries` select file, directory, and combined discovery;
they are mutually exclusive, all operands are roots, and `-g` filters names.
Directory discovery includes empty directories. No roots means `effective_cwd`;
explicit empty roots and stdin reject. Relative roots use that cwd, absolute roots
are allowed, and symlink spelling stays usable. Omitting name filters lists every
eligible entry. `--help` and `--type-list` provide on-demand references.

The option catalog in `_search_options.py` owns aliases, arity, repeat/order
semantics, native forwarding, applicability, validation, and on-demand help.
This is a bounded search interface, never a shell command or unrestricted native
passthrough. The old `action`/`patterns`/`paths`/`options`/`kind` fields are no longer
part of the callable interface; historical persisted rows remain readable.
Supported families cover case/literal/word/line matching, PCRE2 and multiline,
contexts, file/count/absence/quiet output, globs and types, ignores, hidden paths,
symlinks, depth/size/filesystem limits, ordering, encoding, CRLF/NUL, binary
handling, and diagnostics. Unknown flags, missing operands, or incompatible
effects reject with a correction. Plain source coordinates and
formatting are fixed; formatting flags already satisfied by output are accepted.

Common scalar/container encodings and known call wrappers use shared repair;
`argv` is an unadvertised alias for `args`. Conflicting aliases and unknown effects
reject. Arguments such as `true`, `false`, `rg`, literal quotes, and shell syntax
remain search payloads, never executable prefixes or flag booleans. Complete
content/path examples in the definition teach the canonical first call.

Before shared scalar-array conversion, the owning `args` normalizer recognizes
double-quoted encoded lists. It preserves regex backslashes omitted from their
JSON escaping, such as field text `["-e","findMe\("]`. Broken list syntax or malformed
lists with ambiguous JSON control/unicode escapes require correction. Valid encoded JSON and
members of actual arrays retain their existing semantics, including literal quotes
and character classes. It never splits a shell command string into arguments or
strips quotes from a scalar search pattern.

## Selection Contract

Content and path searches share traversal, union/deduplication, filters, and ignores.
Hidden paths are included by default. `.git` files and directories are always excluded.
Explicit ignored roots opt their subtree into searching. Other ignore sources,
from lower to higher precedence: global Git excludes and repository info/exclude,
applicable `.gitignore` files, `.ignore`, `.rgignore`, then extra ignore files.
Nearest repository boundaries include worktree pointer files and common excludes.
Controls can disable sources individually; unreadable rules never silently widen
the scope. Positive vBot filters narrow selection and cannot override ignores.

Name globs are case-insensitive unless explicitly changed. Bare `-g '*.py'` filters
basenames at any depth; globs containing `/` are root-relative, so `-g './*.py'`
selects top-level files. `-i`/`-s` control content case in content searches and glob
case in path discovery; explicit glob-case options also remain available.
Brace alternatives and character classes are supported. Ordered positive and
negative `-g`/`--iglob` filters apply to roots independently; negatives can exclude
directory descendants. File type/size filters on path searches require `--files`.
Overlapping roots deduplicate lexical paths; following symlinks remains opt-in
except an explicit root, preserves its spelling, and detects ancestor loops.

Default content ordering is path ascending; path discovery uses newest modification
first, with path tie-breaks. Explicit sorts cover path, modified, accessed, created,
and unsorted discovery; unsupported creation timestamps reject. A call-scoped
SQLite spool keeps union, deduplication, and sorting off unbounded Python lists.

## Results and Resource Bounds

Success returns `data.content` and `complete`. Paths are relative to effective cwd
when possible, absolute otherwise; directory rows end in `/`. Content includes
source line numbers, and occurrence output adds byte columns. No matches is success.
Quiet returns `matched=true/false`, or null when an incomplete scan proves neither.
`complete` describes scan completion, not whether the returned page contains all
matches; a successful complete scan may still return `next_offset`. When no result
was observed, `searched_paths` contains absolute resolved roots with forward slashes
and `patterns` contains the actual interpreted patterns, including literal quotes.
When investigating false negatives, check the effective cwd, literal quote or
escape characters in patterns, and subsequent narrowed searches before attributing
the outcome to the native engine.

`limit` defaults to 100 (maximum 10,000); `offset` defaults to zero (maximum
1,000,000). Logical matches or path/count rows define pages; context does not
consume match slots. `next_offset` and a continuation instruction appear when
another result was observed. Continuation repeats a live query; filesystem edits
can change page boundaries. Long lines contain marked excerpts around the match;
context omission is explicit and never prevents continuation progress.

The shared 30-second SearchBudget polls traversal and native output, including
silent children. User cancellation kills the child and returns cancelled_by_user;
timeouts, Run cancellation, and unreadable entries make results incomplete with
bounded warnings. Regex errors fail even when selection is empty. Native exit 1
means no match; native diagnostics cannot become a successful empty search.

Independent bounds cover 50 KiB content output, 8 MiB native protocol records,
bounded pipe queues/stderr, 512 MiB child RSS, candidate storage (128 MiB), one
million observed entries, glob expansion, and process arguments. Failures and
exhausted bounds report actionable scope reductions. No persistent search handle
or candidate database survives the call. UI display includes the argument vector
and a result-count fact; detail views retain warnings and continuation metadata.

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

Only `search_files` is registered. New Project ceilings include it; persisted
selections are not auto-migrated. A retired grep/glob denial vetoes the union Tool.
Claude/OpenCode scanner denials for either capability map to search_files.
`python scripts/converters/search_files_access.py <data-dir>` previews manual
Agent/Project policy conversion; `--apply` writes after complete preflight. Mixed
grants/denials require an explicit user choice and never silently widen access.
Historical grep/glob chat rows remain readable.

## Verification

Primary tests: `tests/core/tools/test_search_files*.py` (including encoded-list,
literal-payload, conflict, and empty-scope regressions in `test_search_files_recovery.py`),
`tests/cli/test_search_runtime.py`, `tests/scripts/test_search_files_access.py`,
`tests/scripts/test_probe_search_files.py`, plus runtime, scanner, Chat, packaging,
and Tool row integration tests. Tests execute the private native engine.

`scripts/probe_provider_tool_call.py --scenario search_files` exposes production
definitions to a fresh Model and dispatches its calls against disposable fixtures,
checking independently prescribed results. `--search-case <id>` selects one case,
and comma-separated ids select a subset. The workflow separates natural first-use
requests from exact-call conformance, including options, repairs, and rejection.
The old shared walker in `core/tools/search.py` remains for Chat file mentions;
its UI discovery contract is separate from search_files.
