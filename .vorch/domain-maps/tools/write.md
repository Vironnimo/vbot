# Write Tool

Creates or replaces a complete UTF-8 text file.

## Interfaces

- Tool name: `write`
- Registration: `register_write_tool(registry, *, file_state)` — the `FileReadState` guard registry is injected (factory `make_write_handler(file_state)`, mirrors the read tool).
- Schema: required `path` and `content`; `additionalProperties: false`.
- Success data includes `message`, resolved `path`, and written byte count.
- Display: summary field `path`; hides `content` from argument details.

## Conventions

- Use `write` for full-file replacement or new files.
- Use `edit` for partial edits or append-like changes.
- Relative paths resolve from `ToolContext.workspace`; absolute paths are allowed.

## Constraints & Gotchas

- Parent directories are created automatically.
- Content is written as UTF-8 text.
- A UTF-8 BOM the existing file already had is preserved: if the target starts with a BOM and the supplied content does not, the BOM is re-prepended (never doubled). This keeps the round-trip with the BOM-stripping `read` tool from silently dropping the marker. New files get no BOM. The syntax check (below) still runs on the BOM-free content.
- Validation and expected filesystem errors return failure envelopes.
- Content dominated by read's `N|` line-number gutter (≥2 consecutive numbered lines) is rejected with a `line_numbered_content` failure, so a model cannot corrupt a file by pasting read output back in. Shared detector: `looks_like_line_numbered_content` in `core/tools/arguments.py`.
- After a successful write, the file is syntax-checked in-process by extension (`.py` via `ast`, `.json`, `.yaml`/`.yml`, `.toml` — all dependency-free). A parse error is **not** blocking (the file is already written) but is surfaced as a `data.syntax_warning` string so the model can fix it next turn. The whole file is new on a write, so any parse error is attributed to this write. Logic lives in `core/tools/syntax_check.py`.
- **Read-before-write guard** (shared with `edit`, see `file_state.md`): overwriting an **existing** file is **blocked** (failure envelope) when it was not read in this session (`file_not_read`) or its `(mtime, size)` changed on disk since the read (`file_modified_since_read`). A **non-existent** target (new file) is exempt — the check only runs when `resolved.exists()`. A successful write restamps the file, so the same session can write again without re-reading. Unlike the syntax check this is a hard block and runs *before* the write.
