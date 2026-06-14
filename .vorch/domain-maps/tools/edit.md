# Edit Tool

Performs exact text replacement inside an existing text file.

## Interfaces

- Tool name: `edit`
- Registration: `register_edit_tool(registry)`
- Schema: required `path`, `old_string`, `new_string`; optional boolean `replace_all`; `additionalProperties: false`.
- Success data includes `message`, resolved `path`, `first_changed_line`, and `replacements`.
- Display: summary field `path`; hides `old_string`, `new_string`, `oldString`, and `newString` from argument details.

## Conventions

- Use `edit` for surgical changes to existing files; use `write` for full-file replacement or creation.
- `old_string` must be non-empty, different from `new_string`, and match exactly.
- Without `replace_all: true`, `old_string` must match uniquely.

## Constraints & Gotchas

- The tool normalizes line endings for match/replacement and preserves the file's line-ending style where practical.
- Missing text, ambiguous matches, validation failures, and expected filesystem errors return failure envelopes.
