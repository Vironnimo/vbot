# Archived Browser Use

The former `browser_use` Extension and `browser` Tool are no longer bundled. Browser automation is owned by the bundled `playwright-cli` Skill through `bash`; read `../skills.md` for the runtime boundary and repository-relative `resources/skills/playwright-cli/SKILL.md` for setup and use.

`archive/browser-use.zip` preserves the complete Extension, focused tests, prior domain map/documentation, and original probe/installer files. It is outside discovery roots and excluded from source distributions. Restore only in a worktree and reconcile shared files with current source. The archived implementation is frozen, not a supported runtime capability.

`tests/core/runtime/test_runtime.py::test_playwright_replaces_archived_browser_extension` verifies that bundled startup has the replacement Skill and neither the old Extension nor Tool/Skill. No migration or deletion of user settings, grants, browser downloads or artifacts accompanies the removal.

A running server needs Extension Reload or restart to retire an already loaded copy. Existing Session history retains old mentions; pinned Skill catalogs refresh at Compaction, while live activation uses the current Skill pool. See `extensions/management.md` and `skills.md` for these lifecycle contracts (paths relative to the domain-maps root).

## Replacement Skill maintenance

`resources/skills/playwright-cli/UPSTREAM.json` records the fixed upstream revision, package version, original file hashes and local adaptation. The bundled Apache-2.0 license and unmodified references stay with the Skill. When updating, fetch a fixed upstream revision, review the complete Agent-facing wording, retain or revise the vBot introduction, and update provenance and notices together; runtime startup does not update these instructions.

Verify changes with the package provenance/reference tests in `tests/core/skills/test_skills.py`, the activation and file-read tests in `tests/core/tools/test_skill.py`, and the bundled-startup test above.
