# Edit Tool

Replaces text inside an existing file, matching `old_string` with controlled fuzziness while always splicing the file's real original bytes.

## Interfaces

- Tool name: `edit`
- Registration: `register_edit_tool(registry)`
- Schema: required `path`, `old_string`, `new_string`; optional boolean `replace_all`; `additionalProperties: false`.
- Success data includes `message`, resolved `path`, `first_changed_line`, and `replacements`.
- Display: summary field `path`; hides `old_string`, `new_string`, `oldString`, and `newString` from argument details.

## Conventions

- Use `edit` for surgical changes to existing files; use `write` for full-file replacement or creation.
- `old_string` must be non-empty and different from `new_string`.
- Without `replace_all: true`, `old_string` must match uniquely (the winning strategy's own ambiguity is terminal — it does not fall through to a looser one).

## Matching (fuzzy)

- Matching lives in `core/tools/fuzzy_match.py` (`replace_fuzzy`), a chain tried in order; the first strategy that finds any match wins: **exact** (literal substring) → **normalized** (CR/CRLF→LF plus a small 1:1 Unicode fold — curly quotes, non-breaking space, en-dash → ASCII; character-level, so it matches within a line) → **line_trimmed** (whole-line match after stripping each line's leading/trailing whitespace, with the replacement **re-indented** to the file's actual indentation so a whitespace-only match never corrupts indentation).
- Non-exact strategies search a normalized copy and map the match back to the original bytes via a per-character span map, so the file's exact characters and CRLF endings are preserved. Line-ending style is applied to `new_string` on every strategy (including exact).
- **Similarity / anchor matching is deliberately excluded** — the tool never replaces text that is merely *similar*. For a destructive op, failing (so the model retries with a better target) beats silently editing the wrong block.

## Constraints & Gotchas

- Missing text, ambiguous matches, validation failures, and expected filesystem errors return failure envelopes.
- `new_string` dominated by read's `N|` line-number gutter is rejected with a `line_numbered_content` failure (it would write line-number prefixes into the file). When a not-found `old_string` itself carries the gutter, the `text_not_found` message points at the gutter rather than generic whitespace advice. Shared detector: `looks_like_line_numbered_content` in `core/tools/arguments.py`.
- After a successful edit, the result is syntax-checked in-process by extension (`.py`/`.json`/`.yaml`/`.yml`/`.toml`). It is non-blocking (the edit is already written) and surfaced as `data.syntax_warning`. The file is parsed both before and after, so a pre-existing syntax error is never blamed on the edit — the message then says the file "was already syntactically invalid". Logic in `core/tools/syntax_check.py`.
