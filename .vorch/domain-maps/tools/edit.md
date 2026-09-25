# Archived Edit Tool

The built-in `edit` Tool is retired. Use `apply_patch` for targeted file changes
and Add File for full-file creation and replacement. `apply_patch` advertises only
`patch`, but runs edit-shaped calls (`file_path`/`old_string`/`new_string`,
`replace_all`, `expected_replacements`, MultiEdit `edits`) when they name one exact
change; see `apply_patch.md`.

`archive/edit.zip` preserves the implementation, focused tests, prior domain map,
and original shared integration/probe files at their repository paths. Its
manifest records the source commit and SHA-256 hashes. The archive is outside
runtime discovery and excluded from source distributions. Restore only in a
worktree and reconcile shared files with current source; the archived Tool is
not a supported runtime capability.

`core/tools/fuzzy_match.py` remains the active matching owner. Bounded change
previews now live in `core/tools/_change_preview.py`; `apply_patch` imports no
archived implementation. See `apply_patch.md` for the active behavior.

Runtime inventory and Provider-definition tests in
`tests/core/runtime/test_runtime.py` verify that startup exposes `apply_patch`
and excludes `edit`. A server restart is required to remove an already loaded
built-in Tool. Existing Session history is not rewritten, and the application
never maps a persisted `edit` grant to `apply_patch`. The Generation 1 converter
replaces `edit` in persisted Tool access; `edit` alone does not grant `apply_patch`
(`database/generation-1-conversion.md` -> Retired Tool names).
