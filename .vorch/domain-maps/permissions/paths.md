# Permission Rules paths

Read when changing how the boundary decides a path: normalization and unsafe forms, links and junctions, how a path pattern covers a subtree, recursive and wildcard accesses, listings that skip refused entries, or case and platform differences. The decision order every check follows is in `../permissions.md` -> Decision order; rule strings in `rule-language.md`.

## Normalization

`normalize_path(path, *, base, home, platform)` gives the lexical and resolved forms exactly as the checks compare them (`_paths.py`):

1. `~`, relative paths joined to `base` (default: the working directory), separators, `.`/`..`, Windows trailing dots and spaces, `\\?\` drive prefixes, 8.3 short names (`GetLongPathNameW` on the longest existing prefix).
2. Unsafe forms are recognized lexically before any filesystem access: network paths (UNC, `\\?\UNC\`, `file://host`), device paths (`\\.\`, `\\?\GLOBALROOT`, NT `\??\`), alternate data streams and drive-relative paths. They are refused (`unsafe_form`, `location` names the form) unless an allow rule without wildcards names the path or a folder above it, and they are never resolved, so a UNC path never contacts SMB.
3. Links and junctions resolve with `os.path.realpath` (non-strict). POSIX resolves the joined path as written, so `link/..` climbs out of the link target as the kernel does; Windows resolves the lexical form, as Win32 does. Both forms must pass every later step; a refusal then names where the link leads (`resolved`).

A restricted check touches the filesystem (`realpath`, 8.3 expansion), and `build` resolves every folder and pattern prefix once.

## Patterns and subtrees

- A path pattern covers what it names and everything below it, with or without wildcards (`{workdir}/secret*` covers `{workdir}/secrets/key.pem`). Matching is case-insensitive on Windows (`casefold`), never on POSIX.
- Tool paths do not expand placeholders: `{workdir}` in a Tool argument is a file name, as in `ToolContext.resolve_path` (`../tools/file_state.md` -> Path resolution); only `~` expands.
- `check_path(..., glob=, subtree=, pruned=)`: `glob` (a relative rule-syntax pattern, `*.md`) and `subtree` (everything below, or below the glob's matches: a recursive delete, copy or listing) also refuse when a protected location or a deny rule could meet what they reach, segment by segment (`**` always can), except a deny below a folder named in `pruned`, which the access leaves out at every depth; `below` then names the denied part. `work_tree`: a git operation replaces the work tree of the repository whose top is `path`, so the rule files below may change (`commands.md` -> Paths).
- `refusable_below(folder, access) -> tuple[str, ...]`: patterns, relative to a folder, of what below it may still be refused (deny rules, `agent.json`, install dirs for writes, the data directory; `**` is the whole folder). Below a permitted folder only these and links leading out can refuse, because bases and allow rules cover whole subtrees.
- `readable_entries(folder, entries)` keeps the `os.DirEntry`s of a listing that may be read and checks only links and junctions unless `refusable_below` names something. Both cost nothing under full access; listings, suggestions and search exclusions use them (`../tools/file_state.md` -> Path resolution, `../tools/search_files.md`).

## Limits

- Hardlinks to files outside the permitted folders are not detected on read: a hardlink is an ordinary file name.
- The library protects only the folders the caller names: which folders count as installation or as readable vBot artifacts (and whether a development checkout is an installation) is the caller's decision (`core/chat/_run_permissions.py`).

## Tests

`tests/core/permissions/test_permission_paths.py` (normalization, unsafe forms without filesystem access, links and junctions; Windows-only cases skip elsewhere) and the subtree, glob and listing cases of `test_permission_boundary.py`.
