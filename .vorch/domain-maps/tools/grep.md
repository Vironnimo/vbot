# Grep Tool

Searches file contents by regex or fixed string.

## Interfaces

- Tool name: `grep`
- Registration: `register_grep_tool(registry)` — registers an async wrapper that runs the sync `grep_handler` via `asyncio.to_thread`, so the ripgrep subprocess / fallback scan never blocks the kernel event loop.
- Schema: required `pattern`; optional `path`, `glob`, `ignore_case`, `literal`, `multiline`, `context`, `limit`, `offset`, `include_ignored`, and `output_mode`. camelCase `ignoreCase` / `includeIgnored` are accepted as aliases and normalized before validation (like `edit`'s aliases).
- `output_mode`: `content`, `files_with_matches`, or `count`.
- Success data returns textual output under `data.content`.
- Display: summary fields `pattern` and `path`.

## Conventions

- Regex mode is default; fixed string mode uses `literal: true`; `multiline: true` lets patterns span lines (`.` matches newlines — rg `--multiline --multiline-dotall`, fallback `re.MULTILINE | re.DOTALL`).
- Relative `path` resolves from `ToolContext.effective_cwd` (the working directory); absolute file or directory paths are allowed.
- Result paths are rendered relative to the **working directory** when the file lies under it, absolute otherwise (`display_search_path` in `core/tools/search.py`) — so every result round-trips directly into a follow-up `read`/`edit` call. The `glob` filter still matches search-root-relative paths, bare names at any depth, **case-insensitively on every platform** (rg gets `--glob-case-insensitive` to match the fallback).
- `offset` skips the first N results before `limit` applies (paging). An offset beyond the total yields `No results at offset N; M matches total.`
- **Ignore semantics (user decision 2026-07-02):** `.gitignore`'d files are skipped by default; `include_ignored: true` opts in. Hidden dotfiles are always searched; `.git` internals are always excluded (rg glob `!**/.git` — matches the entry itself, so it also covers a worktree's `.git` pointer *file*; the fallback walker skips both forms). rg runs with `--hidden --no-require-git` (honors `.gitignore` outside git repos too, matching the fallback); the fallback uses the shared walker + `GitIgnoreFilter` (nested `.gitignore`s, deepest-match-wins, parent files honored up to the repo top). An explicitly named file is always searched, and an explicitly targeted ignored directory auto-disables the rules (`ignore_rules_apply`). Residual divergence: rg additionally honors `.ignore`/`.rgignore` and global git excludes; the fallback does not.
- **Worktree boundary:** a `.git` pointer file bounds gitignore evaluation exactly like a `.git` directory (`_find_repository_top` uses `.exists()`), so a worktree living in the main repo's gitignored `.worktrees/` folder is searched normally from inside — the parent repo's `.worktrees/` rule never blanks it out. Verified against real rg 15 behavior; regression-tested for the fallback.

## Constraints & Gotchas

- `rg`/ripgrep may be used when available, but the Python fallback must work without ripgrep. rg output is **streamed**: reading stops at `offset`+`limit`+1 results or the 50 KB byte cap and the process is killed — never buffer a full rg run in memory.
- **Regex dialect is decided by the executing engine**: Rust regex under rg, Python `re` in the fallback. The Python pre-compile no longer gates rg — a pattern like `\p{Lu}` (invalid in Python, valid in Rust) runs fine under rg, and an rg `regex parse error` (e.g. look-around) is surfaced as an `invalid_regex` failure, not `grep_error`. Only when the fallback actually runs does a Python compile error fail the call.
- Both paths are bounded by a `SearchBudget` (`core/tools/search.py`): user cancel returns a `cancelled_by_user` failure envelope (the per-call cancel hook also kills the rg process); run cancel or the shared timeout (`SEARCH_TIMEOUT_SECONDS`, 30s — a `threading.Timer` watchdog covers a silently blocking rg) stops the search and returns partial results with `[Search timed out; results may be incomplete.]`. A kill surfaces to the reader as plain EOF, so the handler polls the budget once after EOF before treating a nonzero rg exit as an error.
- In multiline content mode, rg counts result **rows** toward `limit` while the fallback counts **matches** (a multi-line match emits several rows) — a known, accepted divergence.
- No-match messages are success envelopes.
- Invalid arguments, invalid regexes, and expected path/search errors return failure envelopes.
