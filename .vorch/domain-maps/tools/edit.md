# Archived Edit Tool

The built-in `edit` Tool is retired. Use `apply_patch` for targeted file changes;
`write` remains available for full-file creation and replacement. There is no
`replace_all` argument in `apply_patch`; repeated replacements require explicit
hunks or a script.

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
built-in Tool. Existing Session history and persisted Tool grants are not
rewritten; removing `edit` does not automatically grant `apply_patch`.
