# Glob Tool

Discovers filesystem paths by glob-style pattern.

## Interfaces

- Tool name: `glob`
- Registration: `register_glob_tool(registry)` — registers an async wrapper that runs the sync `glob_handler` via `asyncio.to_thread`, so a large tree walk never blocks the kernel event loop.
- Schema: required `pattern`; optional `path` and `limit` (default 100, min 1); `additionalProperties: false`.
- Success data returns textual matches under `data.content`.
- Display: summary field `pattern`.

## Conventions

- Relative patterns such as `**/*.py` are supported.
- Relative `path` resolves from `ToolContext.effective_cwd` (the working directory); absolute search roots are allowed.
- Result paths are rendered relative to the **working directory** when the match lies under it, absolute otherwise (`display_search_path` in `core/tools/search.py`) — so every result round-trips directly into a follow-up `read`/`edit` call regardless of the search root.
- Matches are sorted by modification time, newest first; equal mtimes tie-break alphabetically. Directory entries end with `/`.

## Constraints & Gotchas

- Results beyond `limit` are cut **and explicitly marked** with `[Results limited to N matches.]`; output is byte-capped at 50 KB with a truncation marker (shared helpers in `core/tools/search.py`).
- The walk polls a `SearchBudget` per entry: user cancel returns a `cancelled_by_user` failure envelope; run cancel or the shared search timeout (`SEARCH_TIMEOUT_SECONDS`, 30s) stops the walk and returns partial results with `[Search timed out; results may be incomplete.]`.
- mtime sorting requires collecting **all** matches before capping — the budget (timeout/cancel) is what bounds a huge walk, not the limit.
- Hidden and ignore-listed files are searched like any others — deliberately no `.gitignore` semantics (user decision 2026-07-02).
- No-match messages are success envelopes, not failures.
- Expected path/search errors return failure envelopes.
