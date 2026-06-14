# Grep Tool

Searches file contents by regex or fixed string.

## Interfaces

- Tool name: `grep`
- Registration: `register_grep_tool(registry)`
- Schema: required `pattern`; optional `path`, `glob`, `ignoreCase`, `literal`, `context`, `limit`, and `output_mode`.
- `output_mode`: `content`, `files_with_matches`, or `count`.
- Success data returns textual output under `data.content`.
- Display: summary fields `pattern` and `path`.

## Conventions

- Regex mode is default; fixed string mode uses `literal: true`.
- Relative `path` resolves from `ToolContext.workspace`; absolute file or directory paths are allowed.
- Optional `glob` limits candidate files before content matching.

## Constraints & Gotchas

- `rg`/ripgrep may be used when available, but the Python fallback must work without ripgrep.
- No-match messages are success envelopes.
- Invalid arguments, invalid regexes, and expected path/search errors return failure envelopes.
