# Write Tool

Creates a file or replaces all of its content. `write` is the whole-file Tool of
the replacement dialect; `edit` (`edit.md`) owns activation, the dialect selection
and the shared ownership notes for both Tools, which live in `core/tools/edit.py`.
History: the retired `write` Tool is preserved in `archive/write.zip`; the current
Tool shares only its name.

## Contract

- Registered by `register_edit_tools` with `edit`: `files` family, follows
  `apply_patch`, hidden from `tool.list`.
- **Schema:** `path` and `content`, both required; unknown root parameters fail at
  dispatch (`write was not run: ... write parameters: path (required), content
  (required).`).
- **Argument repair** (`normalize_write_arguments`, refusals end `No file was
  changed.`): `file_path`/`filename` -> `path`; `text`/`contents`/`file_text` ->
  `content`, any case or separator spelling. `old_string`, `new_string`,
  `replace_all` or `edits` without `content` refuse naming `edit`; a missing path
  or content refuses naming the field (`""` empties the file).
- **Semantics** are `apply_patch`'s Add File (`_patch_requests.content_operation`,
  one step of the shared pipeline): parent folders are created; replacing existing
  content that is not empty or whitespace-only needs a current Session read stamp
  (`file_not_read`/`file_modified_since_read`; a small text file is shown whole and
  stamped, so the same call succeeds when sent again; `file_state.md`); identical
  content is a verified no-op without a read; a new file keeps the content's line
  endings, a replaced file keeps its own line-ending style, BOM and permission
  bits; content whose every line carries a consecutive `read` gutter loses the
  gutters with a note, as in Add File; NUL text fails `binary_file`. Results:
  `Created X (N lines).`,
  `Replaced the content of X (N lines).`, `X already has this content. No file was
  changed.` A file changed on disk while the call ran fails `file_changed`:
  `X changed on disk while this write ran. Read it before writing it again.`
- Coverage: `tests/core/tools/test_edit_tools.py`
  (`test_write_creates_replaces_after_a_read_and_keeps_identical_content`, the
  failure and refusal inventories, the race test).

## Agent-facing text

Minimal by user decision (2026-10-04); about 58 tokens (`scripts.tool_lab
definitions` estimate). No read-first sentence: `To replace an existing file, read
it first.` made 2 of 4 eval runs read a file they were about to create, then list
folders (1-4 extra calls; agent-eval 2026-10-04), while a blind replacement costs
the same two calls through the `file_not_read` refusal, which shows the content and
counts it as read.

| Text | Reason |
|---|---|
| `Create a file or replace all of its content.` | Names both effects; replacing part of a file belongs to `edit`, whose name says so. |
| `path`: `File to write.` | Role; both path forms are accepted, so no format text is needed. |
| `content`: `Complete file content.` | Says the value is the whole file, so an Agent does not send a fragment or a diff. |
| `write replaces the whole file and has no old_string. To replace text inside a file, call edit with path and edits.` | Edit-shaped calls sent to `write` name the Tool that takes them instead of failing on unknown parameters. |
