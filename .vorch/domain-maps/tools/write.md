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
