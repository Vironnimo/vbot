# File Search Tool

Read when changing file/content search, search options, native engine provisioning,
search permission consolidation, or result continuation.

## Ownership and Interface

`core/tools/search_files.py` owns the single `search_files` Tool, registration,
normalization, orchestration, and display. Its private `_search_*` modules own
option parsing, native execution, ignore rules, candidate selection, and results;
these are implementation units of the existing Tools owner, not public services.
The async handler offloads the complete operation through `run_tool_worker`.

Seven advertised fields: required `action` (`content`, `paths`, `help`), plus
`patterns`, `paths`, `options`, `kind`, `limit`, and `offset`. Patterns and literal
roots are arrays. Content patterns are ORed regexes by default; `-F` selects
literal matching. Path patterns are ORed anchored root-relative globs. An omitted
paths array searches `effective_cwd`; explicit empty roots reject. Relative roots
use that cwd, absolute roots are allowed, and symlink spelling stays usable.
`kind` applies only to paths (`files`, `directories`, `all`, default all), including
empty directories. Omitting path patterns lists every eligible entry.

`options` is a token array parsed by `_search_options.py`, never a shell command
or unrestricted native passthrough. The catalog owns aliases, arity, repeat/order
semantics, native forwarding, applicability, validation, and on-demand help.
Supported families cover case/literal/word/line matching, PCRE2 and multiline,
contexts, file/count/absence/quiet output, globs and types, ignores, hidden paths,
symlinks, depth/size/filesystem limits, ordering, encoding, CRLF/NUL, binary
handling, and diagnostics. Unknown flags, operands in the wrong field,
or incompatible effects reject with a correction. Plain source coordinates and
formatting are fixed; formatting flags already satisfied by output are accepted.

Common scalar/container/boolean encodings use shared repair. Explicit aliases
include `pattern`, `path`, and `args`; familiar grep fields and known flag keys
convert only when their effects do not conflict with `options`. A standalone
flag followed by `true` is accepted; `false` requires its explicit reverse flag.
Never drop an unknown effect or silently override a separate explicit constraint.

## Selection Contract

Both actions share traversal, union/deduplication, filters, and ignores. Hidden
paths are included by default. `.git` files and directories are always excluded.
Explicit ignored roots opt their subtree into searching. Other ignore sources,
from lower to higher precedence: global Git excludes and repository info/exclude,
applicable `.gitignore` files, `.ignore`, `.rgignore`, then extra ignore files.
Nearest repository boundaries include worktree pointer files and common excludes.
Controls can disable sources individually; unreadable rules never silently widen
the scope. Positive vBot filters narrow selection and cannot override ignores.

Path globs are case-insensitive unless explicitly changed: `*.py` is top-level,
`**/*.py` includes every depth. Bare `-g '*.py'` filters basenames at any depth.
Brace alternatives and character classes are supported. Ordered positive and
negative `-g`/`--iglob` filters apply to roots independently; negatives can exclude
directory descendants. File type/size filters on path searches require kind=files.
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
or candidate database survives the call. UI display includes patterns, roots, and
a result-count fact; detail views retain warnings and continuation metadata.

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

Primary tests: `tests/core/tools/test_search_files*.py`,
`tests/cli/test_search_runtime.py`, `tests/scripts/test_search_files_access.py`,
`tests/scripts/test_probe_search_files.py`, plus runtime, scanner, Chat, packaging,
and Tool row integration tests. Tests execute the private native engine.

`scripts/probe_provider_tool_call.py --scenario search_files` exposes production
definitions to a fresh Model and dispatches its calls against disposable fixtures,
checking independently prescribed results. `--search-case <id>` selects one case;
the workflow covers natural requests, option combinations, repairs, and rejection.
The old shared walker in `core/tools/search.py` remains for Chat file mentions;
its UI discovery contract is separate from search_files.
