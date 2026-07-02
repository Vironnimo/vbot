# Grep Tool

Searches file contents by regex or fixed string.

## Interfaces

- Tool name: `grep`
- Registration: `register_grep_tool(registry)` — registers an async wrapper that runs the sync `grep_handler` via `asyncio.to_thread`, so the ripgrep subprocess / fallback scan never blocks the kernel event loop.
- Schema: required `pattern`; optional `path`, `glob`, `ignore_case`, `literal`, `context`, `limit`, and `output_mode`. The legacy camelCase `ignoreCase` is accepted as an alias and normalized before validation (like `edit`'s aliases).
- `output_mode`: `content`, `files_with_matches`, or `count`.
- Success data returns textual output under `data.content`.
- Display: summary fields `pattern` and `path`.

## Conventions

- Regex mode is default; fixed string mode uses `literal: true`.
- Relative `path` resolves from `ToolContext.effective_cwd` (the working directory); absolute file or directory paths are allowed.
- Result paths are rendered relative to the **working directory** when the file lies under it, absolute otherwise (`display_search_path` in `core/tools/search.py`) — so every result round-trips directly into a follow-up `read`/`edit` call regardless of the search root. The `glob` filter still matches search-root-relative paths.
- Optional `glob` limits candidate files before content matching.

## Constraints & Gotchas

- `rg`/ripgrep may be used when available, but the Python fallback must work without ripgrep. rg output is **streamed**: reading stops at `limit`+1 results or the 50 KB byte cap and the process is killed — never buffer a full rg run in memory.
- Both paths are bounded by a `SearchBudget` (`core/tools/search.py`): user cancel returns a `cancelled_by_user` failure envelope (the per-call cancel hook also kills the rg process); run cancel or the shared timeout (`SEARCH_TIMEOUT_SECONDS`, 30s — a `threading.Timer` watchdog covers a silently blocking rg) stops the search and returns partial results with `[Search timed out; results may be incomplete.]`. A kill surfaces to the reader as plain EOF, so the handler polls the budget once after EOF before treating a nonzero rg exit as an error.
- rg runs with `--hidden --no-ignore --text`: hidden and ignore-listed files are searched deliberately — no `.gitignore` semantics (user decision 2026-07-02).
- No-match messages are success envelopes.
- Invalid arguments, invalid regexes, and expected path/search errors return failure envelopes.
