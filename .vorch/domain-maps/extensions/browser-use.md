# Archived Browser Use

The former `browser_use` Extension and `browser` Tool are no longer bundled. Browser automation is owned by the bundled `playwright-cli` Skill through `bash`; read `../skills.md` and repository-relative `docs/browser-use.md` for the current boundary.

`archive/browser-use.zip` preserves the complete Extension, focused tests, prior domain map/documentation, and original probe/installer files. It is outside discovery roots and excluded from source distributions. Restore only in a worktree and reconcile shared files with current source. The archived implementation is frozen, not a supported runtime capability.

`tests/core/runtime/test_runtime.py::test_playwright_replaces_archived_browser_extension` verifies that bundled startup has the replacement Skill and neither the old Extension nor Tool/Skill. No migration or deletion of user settings, grants, browser downloads or artifacts accompanies the removal.
