# Glob Tool

Discovers filesystem paths by glob-style pattern.

## Interfaces

- Tool name: `glob`
- Registration: `register_glob_tool(registry)` — registers an async wrapper that runs the sync `glob_handler` via `asyncio.to_thread`, so a large tree walk never blocks the kernel event loop.
- Schema: required `pattern`; optional `path`, `limit` (default 100, min 1), `offset` (default 0), and `include_ignored` (default false). Defaults are handler-owned and stated only in descriptions; the model-facing schema omits `additionalProperties`, and the handler rejects unknown fields including legacy/camelCase spellings.
- Success data returns textual matches under `data.content`.
- Display: the primary is the quoted `pattern`. Every successful page publishes its displayed path count as a presentation-only `results` fact; a limited or timed-out non-empty page sets `at_least: true`, while failures publish no count. The Agent-visible success envelope remains unchanged.

## Conventions

- Pattern matching is **anchored** standard glob (`glob_path_matches` in `core/tools/search.py`): `*.py` matches top-level entries only, `**/*.py` at any depth — unlike grep's `glob` filter, which matches bare names at any depth. Matching is **case-insensitive on every platform** (deliberate: identical behavior on Windows dev and Linux deployment).
- Relative `path` resolves from `ToolContext.effective_cwd` (the working directory); absolute search roots are allowed.
- Result paths are rendered relative to the **working directory** when the match lies under it, absolute otherwise (`display_search_path`) — so every result round-trips directly into a follow-up `read`/`edit` call regardless of the search root.
- Matches are sorted by modification time, newest first; equal mtimes tie-break alphabetically. Directory entries end with `/`.
- **Ignore semantics (user decision 2026-07-02):** `.gitignore`'d paths are skipped by default (shared walker `iter_search_entries` + `GitIgnoreFilter`); `include_ignored: true` opts in. Hidden dotfiles are always matched; `.git` is always pruned in both forms (directory and a worktree's pointer file). Explicitly targeting an ignored directory as `path` auto-disables the rules (`ignore_rules_apply`) instead of returning a misleading empty result.
- **Worktree boundary:** a `.git` pointer file bounds gitignore evaluation like a `.git` directory, so a worktree inside the main repo's gitignored `.worktrees/` folder matches normally from inside — the parent repo's rule never blanks it out (regression-tested).

## Constraints & Gotchas

- Results beyond `offset`+`limit` are cut **and explicitly marked** with `[Results limited to N matches.]`; output is byte-capped at 50 KB with a truncation marker. An offset beyond the total yields `No results at offset N; M matches total.` — not a bare no-match message.
- The walk polls a `SearchBudget` per entry: user cancel returns a `cancelled_by_user` failure envelope; run cancel or the shared search timeout (`SEARCH_TIMEOUT_SECONDS`, 30s) stops the walk and returns partial results with `[Search timed out; results may be incomplete.]`.
- mtime sorting requires collecting **all** matches before capping — the budget (timeout/cancel) is what bounds a huge walk, not the limit.
- No-match messages are success envelopes, not failures.
- Expected path/search errors return failure envelopes.
