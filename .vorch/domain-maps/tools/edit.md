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
  only `apply_patch` (one user-visible switch). A fixed Tool set
  (`ToolAccess.fixed`) with `apply_patch` gets them as well. A persisted `denied: ["edit"]` still vetoes `edit`; the route then
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
  refusal and `search_files`' `--replace` note use it).
- **Schema:** `path` plus `edits` (at least one item of `old_string`, `new_string`,
  optional `replace_all`), open model-facing schema; unknown root parameters fail
  at dispatch (`edit was not run: ... edit parameters: path (required), edits
  (required).`).
- **Argument repair** (`normalize_edit_arguments`, wrapped by `_refusing`, so every
  refusal is `invalid_arguments` ending `No file was changed.`): `file_path`/
  `filename` -> `path`, `old_str`/`old_text`/`oldText` and the `new_*` twins,
  any case or separator spelling (`filePath`, `oldString`, `replaceAll`); `edits`
  or one of its items sent as JSON text, also with unescaped `"` inside
  `old_string`/`new_string` (`decode_json_argument`, the shared quote repair in
  `tools.md` -> Shared argument repair, against the schema of an edit; 5 of 12
  real stringified `edits` decoded this way, all as the reviewed reading). Text
  that is not JSON at all is refused with `edits arrived as text, not as an
  array. ...`; broken JSON text is refused with where it breaks (character,
  item and field, the text around it, how to write it) and, for a break inside
  a text, `If this text fails again, send one edit per call, or call write with
  the whole file.` A flat `old_string`/`new_string` call is one edit; an item's
  own `path` is accepted when it names the call's file (or supplies the missing
  root path) and refused when it names another file. Refused before any effect,
  each naming the call to send: `edits` beside flat fields, a root `replace_all`
  beside `edits`, `content` (points to `write`), no path, no change, a half pair,
  an item that is not an object, an unknown item field, a field given twice with
  different values. Coverage:
  `test_other_edit_spellings_make_the_same_change`,
  `test_edits_text_with_unescaped_quotes_runs_only_when_it_has_one_reading`,
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
- **Results** are `apply_patch`'s (`_patch_report.patch_result`, `{content}`):
  `Updated X (places).`, with the changed regions as they are now only when the
  written text differs from the call or the call was partial (`apply_patch.md`),
  `Created X (N lines).`, notes and syntax warnings; a note for identical old/new
  text, and
  `X already reads as this edit would leave it.` (`these edits`) when the edits
  leave the file as it was, such as a change and its reversal (the
  `already_applied` template).
  An edit whose non-empty `new_string` is its `old_string` without the final
  line break changes nothing else; when the file continues after the match, the
  call is refused before any effect (`line_join`, `_edit_engine._refuse_join`,
  also for `apply_patch`'s `old_string` shapes): the edit would only join the
  next line to the matched text's last line, or delete the blank line after it.
  The refusal quotes both lines, at most 100 characters each, and says to leave
  the edit out or, to join the lines, to include the next line in `old_string`
  and `new_string`. Every such edit of the call is named in the one refusal
  (`also` details, `Edit K: new_string is likewise ...`), and the closing names
  them all (`with edits 2 and 3 corrected or left out`). At the end of the file
  the edit applies: it only drops the final line break. Evidence: in the
  recorded Sessions (2026-10), 9 such items in 7 successful calls, mostly from
  one Model, joined code lines (twice breaking Python syntax) or deleted blank
  lines, none on purpose; the Agents spent calls on reading and repairing them.
  An edit that also changes text and drops the final line break joins the next
  line to its last line; when that line is not blank and not the end of the
  file, a note quotes the joined line, at most 100 characters on each side of
  the join (`_joined_lines`). That edit applies as sent: some joins are meant.
  Coverage: `test_edits_that_leave_the_file_as_it_is_say_so`,
  `test_an_edit_that_drops_a_final_line_break_names_the_joined_line`. The
  display shows the path and the shared `file_changes` diff and notices.
- **Failures** use the engine's messages with `edit` wording where they name the
  Tool (`_EDIT_TEMPLATES`) and a closing that names the edit to fix
  (`_edit_failure`): one edit -> `No file was changed.`; several -> the failed edit
  is labeled `edit K of N` and the closing says no edit was applied and to send all
  edits again with edit K corrected or left out (left out when its change is
  already in the file). A match report for edit K > 1 shows the text the earlier
  edits left, not the file, and says so where it names lines (`The closest text
  in the file after edits 1-2, ...`, `First difference: after edits 1-2, line N
  reads ...`, `Where it occurs after edit 1:`); when shown lines are ones an
  earlier edit of the call wrote, it names that edit (`Edit 2 of this call wrote
  line N.`; `apply_hunks` tracks each edit's written lines through the later
  ones). An `old_string` whose lines the file holds whole, each part at exactly
  one place, but at places apart (not in order with at most 3 non-blank lines
  left out between them) names the places instead of a closest text: `old_string
  joins lines from different places in the file: its lines 1-3 are lines 60-62
  and its lines 5-6 are lines 41-42. Give each place its own edit.` plus those
  lines (`_edit_engine._split_copy`; lines compare with whitespace collapsed).
  Each part needs 2 lines with a word of 3 or more characters: in a replay of
  582 failed calls (2026-10), 4 of 8 such reports rested on single lines, one of
  them `return tool_success(result)` of another function; the first difference
  served those copies better. The other 4 each joined two functions or blocks the
  Agent took for neighbors. Coverage:
  `test_failed_changes_change_nothing_and_say_what_to_send`,
  `test_a_file_changed_during_the_call_is_not_overwritten`.

## Agent-facing text

Texts are minimal by user decision (2026-10-04): the boundary between `edit` and
`write` comes from the names, details belong in results and errors. About 143
tokens (`python -m scripts.tool_lab definitions` estimate, mostly schema); `write`
adds about 68, against about 53 for `apply_patch`. A sentence stays only when a
fresh Agent would often make a failing call without it; occasional extra
verification calls do not justify text every request pays for (user decision
2026-10-04). Parameter descriptions name the role; format sentences are left out
where every form a Model sends is accepted (relative and absolute paths).

| Text | Reason |
|---|---|
| `Replace text in a file.` | Names the effect; what to do with a whole file comes from `write`'s name and the `content` refusal. |
| `Calls that change different files never need each other finished first; send them in the same response.` | Batching changes is a user requirement. "Calls that change" covers `write` calls too, which the eval failure (a new file before the edit that imports it) involved; the former "Calls for different files" read as `edit` calls only (review 2026-10-10). Without it, 3 of 8 multi-file eval runs sent the calls for a second file in a later response (a new file before the edit that imports it, a definition before its callers), reading the System Prompt's hold-back rule as a dependency; one round trip each (agent-eval 2026-10-05). |
| `edits`: `All changes to this file.` | Puts every change to the file into one call instead of one call per change. Later edits that build on earlier ones and items naming another file fail with the fix. |
| `edits[].old_string`: `Text to replace. Without replace_all, it must occur exactly once.` | A repeated `old_string` is the common failing edit; the rule prevents it before the first call. The `replace_all` condition keeps a literal reader from padding an `old_string` it means to replace everywhere. |
| `edits[].replace_all`: `Replace every occurrence. Omit to replace one.` | Weak Models fill every field; a filled `replace_all: true` changes text the Agent did not mean (F3). |
| `path`, `edits[].new_string`: no text | The names say the role, both path forms are accepted, and `""` deleting is trained behavior (review 2026-10-10). |
| `ambiguous_replacement`: `old_string matches N places (lines ...). Include more of the surrounding text so it matches once, or set replace_all to true to change every match.` | "matches" holds also when only whitespace-tolerant matching found the places; "occurs" was false there (review 2026-10-04). |
| `write`'s `file_not_read` closing: `... send the same call again to replace it, or call edit to change only part of it.` | Names the Tool for the other valid intent (`partial_change` template). |
| Match report for edit K > 1: `The closest text in the file after edits 1-4, line 287:`, `First difference: after edits 1-4, line 287 reads '...' where old_string has '...'.`, `Edit 2 of this call wrote line 287.` | Replaces `the file has` plus a trailing `Line numbers count the text as edits 1-4 left it.`: the report said the file had `Param(1)` at line 287, which edit 2 of the same call had written; the file held `Param(0)` (Swarm Run 2026-10). The trailing sentence came after the text it qualified, and naming the edit shows that the failed edit's place overlaps a change the call already makes. |
| Split report: `old_string joins lines from different places in the file: its lines 1-3 are lines 60-62 and its lines 5-6 are lines 41-42. Give each place its own edit.` | An `old_string` that joined file lines 60-62 and 41-42 got the closest texts and a first difference at line 37, which named neither cause; the Agent rewrote the whole 142-line file with `write` instead (Swarm Run 2026-10). A passage that only left out a few lines keeps its first difference, which names the missing line. |
| Joined-line note: `new_string does not end with a line break as old_string does, so the next line now continues its last line: import a2 from 'a';import b from 'b';` | In the recorded Sessions (2026-10), 4 of 2751 edit items joined two non-empty lines this way, none on purpose; the result said only `Updated X (line N).`, since the written text was what the call sent. Three cost 2 to 3 more calls each to find and undo the join (reads, a failed edit, the fix); the fourth, a joined TypeScript import, stayed in the file. The quoted line is a ready `old_string` for the fix; no line number, because a later edit of the same call can move it. |
| `line_join`: `X, edit K of N: new_string is old_string without its final line break, so this edit would only join the line '...' with the next line, '...'. Leave this edit out; to join the two lines, include the next line in old_string and new_string.` | 9 items in 7 calls dropped only the final line break (Sessions, 2026-10); each joined code lines or deleted a blank line, none on purpose, and the result said only `Updated X (line N).`. The edit changes nothing else, so leaving it out is the usual fix; the second clause keeps an intended join possible without guessing. Quoting the lines instead of numbering them stays right when earlier edits of the call moved lines. |
| `blank_line_join`: `... so this edit would only delete the blank line after the line '...'. Leave this edit out; to delete that blank line, end old_string with two line breaks and new_string with one.` | The same shape before a blank line deletes it. "Include the next line" would invite `old_string` `X\n\n` with `new_string` `X`, which joins the line after the blank one; the counted line breaks name the edit that deletes only the blank line. |
| `Edit K: new_string is likewise old_string without its final line break; it would only join ...` plus the closing `Send all N edits again with edits J and K corrected or left out.` | One of the 7 calls had two such items; naming only the first would cost a second refused call. |
| `file_exists`: `...old_string is empty, which creates a file, but the file already has content. Put the current text to replace in old_string, or call write to replace the whole file.` | An empty `old_string` is the creation form; on an existing file both valid intents are named. |
| Broken `edits` JSON text: `edits is not valid JSON. At character N, <what breaks in which field>:` + the text around it + how to write it | The former `edits arrived as text, not as an array.` named no break, so the Model could not tell what to fix; 12 `edits` texts in the live Sessions failed this way, 7 of them still after the quote repair (tool review 2026-10-11). |
| `If this text fails again, send one edit per call, or call write with the whole file.` | Only for a break inside a text (a quote, an escape, a line break): long code with quotes is what Models fail to escape; a smaller call or `write` avoids the nested JSON. Dispatch does not know whether the call repeats, so the sentence is conditional. |

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
