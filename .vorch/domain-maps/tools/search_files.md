# Archived File Search Tool

Local file and content searches run through the shell Tool with the bundled
ripgrep executable. Shell behavior and Permission Rules are documented in
`tools/shell.md` and `permissions/commands.md`.

`archive/search_files.zip` preserves the retired Tool at its last active
commit. It includes its definition, argument handling, native engine adapter,
permission checks, result rendering, focused tests, Provider probes and
previous documentation. Shared registration, package, Agent configuration and
WebUI files are included at their original repository paths. `MANIFEST.json`
records the Git commit and the SHA-256 of every archived source file.
Restore in a worktree and reconcile shared files with the current checkout.

The Chat composer's `@` picker still uses ripgrep directly. Its bounded
listing in `core/chat/_file_picker.py` includes hidden files, applies ignore
rules even outside a Git repository, and excludes Git metadata and Python
bytecode caches. Directory browsing also offers the ignored entries, marked
as such. This selection belongs to the picker and is independent of shell
command defaults.
