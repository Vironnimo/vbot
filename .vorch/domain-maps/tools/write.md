# Archived Write Tool

The built-in `write` Tool is retired. Use `apply_patch` with `*** Add File: path`
and `+` content lines to create or fully replace a file. An existing file must
have been read in the current Session and remain unchanged since that read;
identical content is a verified no-op. Write-shaped calls (`file_path` plus
`content`) run as the same full replacement. See `apply_patch.md` and `file_state.md`.

`archive/write.zip` preserves the implementation, focused tests, prior domain map,
and original shared integration/probe files at their repository paths. Its
manifest records the source commit and SHA-256 hashes. The archive is outside
runtime discovery and excluded from source distributions. Restore only in a
worktree and reconcile shared files with current source.

Runtime inventory and Provider-definition tests verify that startup exposes
`apply_patch` and excludes `write`. Restart the server to remove an already
loaded Tool. Existing Session history is not rewritten, and the application
never maps a persisted `write` grant to `apply_patch`. The Generation 1 converter
replaces `write` with `apply_patch` in persisted Tool access, and a denied `write`
with a denied `apply_patch` (`database/generation-1-conversion.md` -> Retired Tool names).
