# Archived capabilities

`browser-use.zip` preserves the retired Browser Use Extension at its original paths,
including its Skill, focused tests, documentation and developer probes. It is a
frozen source archive, excluded from application discovery and source distributions.
See [Archived Browser Use](../.vorch/domain-maps/extensions/browser-use.md) for restoration and replacement details. Do not extract
shared scripts over a current checkout without reconciling their changes.

`edit.zip` preserves the retired `edit` Tool, focused tests, prior documentation,
and original shared integration/probe files. Its manifest records the source
commit and content hashes. See [Archived Edit Tool](../.vorch/domain-maps/tools/edit.md)
for the `apply_patch` replacement and restoration boundary.

`write.zip` preserves the retired `write` Tool, focused tests, prior documentation,
and original shared integration/probe files with source commit and SHA-256 hashes.
See [Archived Write Tool](../.vorch/domain-maps/tools/write.md) for replacement and restoration.

`quality-gates.zip` preserves the retired `scripts/quality.py` and
`scripts/quality-frontend.py` runners with their shared helper, documentation,
tests and the integration files that invoked them, with source commit and SHA-256
hashes. The tracked pre-commit hook, targeted test runs and CI on every push to
`main` replace them (`.vorch/PROJECT.md` -> Testing). Restore only in a worktree.
