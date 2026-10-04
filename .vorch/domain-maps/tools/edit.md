# Edit Tool

Replaces exact text in one file, several edits per call, applied in order as one
atomic change. `edit` and `write` (`write.md`) form the replacement dialect of the
file edit Tools; `apply_patch` (`apply_patch.md`) is the patch dialect. Chat offers
one dialect per prompt epoch, chosen by the primary route's Model family
(`chat/request-building.md` -> Tool catalog per prompt epoch).
`core/tools/edit.py` owns both Tools' definitions, argument repair, result and
failure wording, registration, and the dialect helpers. The shared change pipeline
(`_file_changes.py`: locks, planning, read guard, atomic writes, reports) and the
engine (`_edit_engine.py`, `copy_match.py`, `fuzzy_match.py`) are shared with
`apply_patch`; their canonical description is `apply_patch.md`.
History: the retired `edit` Tool is preserved in `archive/edit.zip`
(`tests/test_archived_edit.py` guards it); the current Tool shares only its name.

## Contract

- **Activation follows `apply_patch`.** `register_edit_tools(registry, *,
  file_state)` registers `edit` and `write` in the `files` family with activation
  `follows` -> `apply_patch` and `catalog_visible=False`: they are active exactly
  when `apply_patch` is, a denial of `apply_patch` removes them, and neither
  appears in `tool.list`, so the Tool picker and the Project Tool Whitelist show
  only `apply_patch` (one user-visible switch). `ToolAccess.fixed` adds no
  followers. A persisted `denied: ["edit"]` still vetoes `edit`; the route then
  keeps `apply_patch` (`offer_edit_dialect`). Coverage:
  `test_edit_tools.py::test_edit_and_write_are_available_exactly_when_apply_patch_is`,
  `test_projects.py` (not in `PROJECT_DEFAULT_ALLOWED_TOOLS`).
- **Dialect helpers** (exported from `core.tools`): `edit_dialect(family)` returns
  `patch` for a casefolded family that starts with `gpt` (not `gpt-oss`), equals
  `o` or starts with `o-`, else `replace` (an unknown family included).
  `known_edit_dialect(names)` reads the dialect an epoch already told the Model
  (`apply_patch` -> `patch`, `edit` or `write` -> `replace`, none -> `None`).
  `offer_edit_dialect(definitions, dialect)` keeps one dialect's definitions;
  `replace` without `edit` falls back to `patch`. `edit_tool_siblings(names)` lists
  the file edit Tools missing from `names` when it holds one; Chat's dispatch
  allowlist adds them, so a call to a sibling the route did not offer runs under its
  own registered contract when policy allows it (`chat/request-building.md` ->
  Dispatch). `offered_edit_tool(context)` names the edit Tool a result may point to
  (`apply_patch`, then `edit`, through `ToolContext.offers`; `read`'s change-command
  refusal and `search_files`' `--replace` note use it). The shell description's
  file-Tool sentence names the first offered of `apply_patch` and `edit` from the
  request's definitions (`project_shell_tool_definitions`, `shell.md`).
- **Schema:** `path` plus `edits` (at least one item of `old_string`, `new_string`,
  optional `replace_all`), open model-facing schema; unknown root parameters fail
  at dispatch (`edit was not run: ... edit parameters: path (required), edits
  (required).`).
- **Argument repair** (`normalize_edit_arguments`, wrapped by `_refusing`, so every
  refusal is `invalid_arguments` ending `No file was changed.`): `file_path`/
  `filename` -> `path`, `old_str`/`old_text`/`oldText` and the `new_*` twins,
  any case or separator spelling (`filePath`, `oldString`, `replaceAll`); `edits`
  or one of its items sent as JSON text (text that encodes no array or object is
  refused: `edits arrived as text, not as an array. ...`); a flat `old_string`/`new_string` call is one edit; an item's
  own `path` is accepted when it names the call's file (or supplies the missing
  root path) and refused when it names another file. Refused before any effect,
  each naming the call to send: `edits` beside flat fields, a root `replace_all`
  beside `edits`, `content` (points to `write`), no path, no change, a half pair,
  an item that is not an object, an unknown item field, a field given twice with
  different values. Coverage:
  `test_other_edit_spellings_make_the_same_change`,
  `test_open_or_misdirected_calls_say_which_call_to_send`.
- **Semantics:** the call is one step of the shared pipeline run with `atomic=True`:
  every edit is planned in memory, in order, each against the text the edits before
  it left, then the file is written once; one failed edit writes nothing. Each
  `old_string` must match exactly one place (substring matching through the
  engine's precise steps, read gutters, then `copy_match`, never `copy_match` with
  `replace_all`; `apply_patch.md` -> Matching and recovery); several matches fail
  `ambiguous_match` with their lines, also after an earlier edit of the call.
  `replace_all` replaces every leftmost non-overlapping occurrence. An empty
  `old_string` creates a missing file (parent folders included) or fills an empty
  or whitespace-only one, and fails `file_exists` otherwise. Changing existing text
  needs no read stamp: the unique match against current bytes is the precondition;
  a file changed since the Session's read adds the stale note to a successful
  result or a match failure (`file_state.md`).
- **Results** are `apply_patch`'s (`_patch_report.patch_result`, `{status,
  content}`): `Updated X:` with the changed regions as they are now, `Created X (N
  lines).`, notes and syntax warnings; `unchanged` for identical old/new text, and
  `X already reads as this edit would leave it.` (`these edits`) when the edits
  leave the file as it was, such as a change and its reversal (the
  `already_applied` template).
  Coverage: `test_edits_that_leave_the_file_as_it_is_say_so`. The
  display shows the path and the shared `file_changes` diff and notices.
- **Failures** use the engine's messages with `edit` wording where they name the
  Tool (`_EDIT_TEMPLATES`) and a closing that names the edit to fix
  (`_edit_failure`): one edit -> `No file was changed.`; several -> the failed edit
  is labeled `edit K of N` and the closing says no edit was applied and to send all
  edits again with edit K corrected or left out (left out when its change is
  already in the file); a match report for edit K > 1 adds that its line numbers
  count the text the earlier edits left. Coverage:
  `test_failed_changes_change_nothing_and_say_what_to_send`,
  `test_a_file_changed_during_the_call_is_not_overwritten`.

## Agent-facing text

Texts are minimal by user decision (2026-10-04): the boundary between `edit` and
`write` comes from the names, details belong in results and errors. About 151
tokens (`python -m scripts.tool_lab definitions` estimate, mostly schema); `write`
adds about 68, against about 53 for `apply_patch`. A sentence stays only when a
fresh Agent would often make a failing call without it; occasional extra
verification calls do not justify text every request pays for (user decision
2026-10-04). Parameter descriptions name the role; format sentences are left out
where every form a Model sends is accepted (relative and absolute paths).

| Text | Reason |
|---|---|
| `Replace text in a file.` | Names the effect; what to do with a whole file comes from `write`'s name and the `content` refusal. |
| `Calls for different files never need each other finished first; send them in the same response.` | Batching changes is a user requirement. Without it, 3 of 8 multi-file eval runs sent the calls for a second file in a later response (a new file before the edit that imports it, a definition before its callers), reading the System Prompt's hold-back rule as a dependency; one round trip each (agent-eval 2026-10-05). |
| `path`: `File to change.` | Role; both path forms are accepted, so no format text is needed. |
| `edits`: `All changes to this file.` | Puts every change to the file into one call instead of one call per change. Later edits that build on earlier ones and items naming another file fail with the fix. |
| `edits[].old_string`: `Text to replace. Without replace_all, it must occur exactly once.` | A repeated `old_string` is the common failing edit; the rule prevents it before the first call. The `replace_all` condition keeps a literal reader from padding an `old_string` it means to replace everywhere. |
| `edits[].new_string`: `Replacement text.` | Role; `""` deleting is trained behavior. |
| `edits[].replace_all`: `Replace every occurrence. Omit to replace one.` | Weak Models fill every field; a filled `replace_all: true` changes text the Agent did not mean (F3). |
| `ambiguous_replacement`: `old_string matches N places (lines ...). Include more of the surrounding text so it matches once, or set replace_all to true to change every match.` | "matches" holds also when only whitespace-tolerant matching found the places; "occurs" was false there (review 2026-10-04). |
| `write`'s `file_not_read` closing: `... send the same call again to replace it, or call edit to change only part of it.` | Names the Tool for the other valid intent (`partial_change` template). |
| `file_exists`: `...old_string is empty, which creates a file, but the file already has content. Put the current text to replace in old_string, or call write to replace the whole file.` | An empty `old_string` is the creation form; on an existing file both valid intents are named. |

## Verification

- `tests/core/tools/test_edit_tools.py`: activation and pinned definitions, the
  dialect table, ordered atomic edits, creation, spellings, the full failure and
  refusal inventories, unknown parameters, a race during the call.
- `tests/core/chat/test_chat_loop_tool_definitions.py` (offered dialect per family,
  epoch pin across a Model change), `test_tool_dispatch.py` (one permission),
  `test_model_names.py` (harness names), `test_read_fields.py`,
  `test_search_files_arguments.py` and `test_shell.py` (texts naming the offered
  edit Tool).
- `scripts/tool_lab/cases/files.json` holds edit and write cases for
  `python -m scripts.tool_lab probe`.
