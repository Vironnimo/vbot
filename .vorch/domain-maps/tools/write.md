# Write Tool

Creates or replaces a complete UTF-8 text file.

## Interfaces

- Tool name: `write`
- Registration: `register_write_tool(registry)`
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
- Validation and expected filesystem errors return failure envelopes.
- Content dominated by read's `N|` line-number gutter (≥2 consecutive numbered lines) is rejected with a `line_numbered_content` failure, so a model cannot corrupt a file by pasting read output back in. Shared detector: `looks_like_line_numbered_content` in `core/tools/arguments.py`.
- After a successful write, the file is syntax-checked in-process by extension (`.py` via `ast`, `.json`, `.yaml`/`.yml`, `.toml` — all dependency-free). A parse error is **not** blocking (the file is already written) but is surfaced as a `data.syntax_warning` string so the model can fix it next turn. The whole file is new on a write, so any parse error is attributed to this write. Logic lives in `core/tools/syntax_check.py`.
