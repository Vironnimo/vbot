# Edit Tool

Replaces text inside an existing file, matching `old_string` with controlled fuzziness against the current on-disk content while always splicing the file's real original bytes.

## Interfaces

- Tool name: `edit`
- Registration: `register_edit_tool(registry, *, file_state)` — the `FileReadState` guard registry is injected (factory `make_edit_handler(file_state)`).
- Model-facing schema: required `path`, `old_string`, `new_string`; optional boolean `replace_all`. It omits `additionalProperties` and the JSON Schema `default` keyword; omission means one unique match, with the handler applying `false` and rejecting unknown or malformed arguments.
- Success data includes `message`, resolved `path`, `first_changed_line`, and `replacements`; the returned path and the same path inside vBot-authored failure text use the shared forward-slash Model presentation.
- Display: summary field `path`; hides `old_string`, `new_string`, `oldString`, and `newString` from argument details.

## Conventions

- Use `edit` for surgical changes to existing files; use `write` for full-file replacement or creation.
- `old_string` must be non-empty and different from `new_string`.
- Without `replace_all: true`, `old_string` must match uniquely (the winning strategy's own ambiguity is terminal — it does not fall through to a looser one). The model-facing contract tells callers to include an unchanged neighboring line or heading when a line may repeat.

## Matching (fuzzy)

- Matching lives in `core/tools/fuzzy_match.py` (`replace_fuzzy`), a chain tried in order; the first strategy that finds any match wins: **exact** (literal substring) → **normalized** (CR/CRLF→LF plus a small 1:1 Unicode fold — curly quotes, non-breaking space, en-dash → ASCII; character-level, so it matches within a line) → **line_trimmed** (whole-line match after stripping each line's leading/trailing whitespace) → **whitespace_normalized** (character-level match after collapsing each horizontal Space/Tab run while preserving line boundaries). The final two strategies re-indent the replacement to the file's actual indentation so a whitespace-only match never corrupts indentation.
- Non-exact strategies search a normalized copy and map the match back to the original bytes via a per-character span map, so the file's exact characters and CRLF endings are preserved. Line-ending style is applied to `new_string` on every strategy (including exact).
- **Similarity / anchor matching is deliberately excluded** — the tool never replaces text that is merely *similar*. For a destructive op, failing (so the model retries with a better target) beats silently editing the wrong block.

## Constraints & Gotchas

- **Current-content optimistic edit:** `edit` does not require a prior `read` and does not block when the file changed since the Session last read it. Under the path's mutation lock it reads the current bytes, applies the unique `old_string` match to those bytes, and preserves every unmatched byte; a changed file whose target no longer matches still fails without writing through the normal `text_not_found` / `ambiguous_match` boundary. A successful edit restamps the file for later full-file `write` checks, and a successful edit after detected metadata drift includes `data.stale_warning` explaining that it merged against newer on-disk content.
- **Serialized atomic mutation** (shared with `write`, see `file_state.md`): one Runtime serializes mutations of the same resolved path while different paths remain independent. The replacement is written to a same-directory temporary file, flushed, permission-matched to an existing target, and installed with atomic `os.replace`; a failed write removes the temporary file and leaves the original intact.
- Missing text, ambiguous matches, validation failures, and expected filesystem errors return failure envelopes.
- An `ambiguous_match` remains a hard no-write result and reports the occurrence count plus at most three raw candidate contexts. Each candidate includes the matched line and one neighboring line on either side, omits read's line-number gutter so its text can be reused safely, and truncates individual context lines at 160 characters; additional candidates are summarized rather than emitted without bound.
- `new_string` dominated by read's `N| ` line-number gutter is rejected with a `line_numbered_content` failure (it would write line-number prefixes into the file). When a not-found `old_string` itself carries the gutter, the `text_not_found` message points at the gutter rather than generic whitespace advice. Shared detector: `looks_like_line_numbered_content` in `core/tools/arguments.py`; it tolerates a reproduced gutter whose separator space was dropped.
- After a successful edit, the result is syntax-checked in-process by extension (`.py`/`.json`/`.yaml`/`.yml`/`.toml`). It is non-blocking (the edit is already written) and surfaced as `data.syntax_warning`. The file is parsed both before and after, so a pre-existing syntax error is never blamed on the edit — the message then says the file "was already syntactically invalid". Logic in `core/tools/syntax_check.py`.
