# Edit Tool

Replaces exact text in one file, several edits per call, applied in order and
written once; an edit that fails is skipped and the others are written. `edit` and
`write` (`write.md`) form the replacement dialect of the
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
  refusal is `invalid_arguments` opening with `No file was changed.`): `file_path`/
  `filename` -> `path`, `old_str`/`old_text`/`oldText` and the `new_*` twins,
  any case or separator spelling (`filePath`, `oldString`, `replaceAll`); `edits`
  or one of its items sent as JSON text, also with unescaped `"` or raw line
  breaks inside `old_string`/`new_string` (`decode_json_argument`, the shared
  quote repair in `tools.md` -> Shared argument repair, against the schema of an
  edit; 5 of 12 real stringified `edits` decoded this way, all as the reviewed
  reading; of the 6 `edits` texts that failed in the Swarm of 2026-10-12, 1
  more decodes with raw line breaks read). Text that is not JSON at all is
  refused with `edits arrived as text, not as an array. ...`; broken JSON text
  is refused with where it breaks (character, item and field, the text around
  it, how to write it) and one closing sentence. When the call also has a root
  `old_string` or `new_string` (the text closed early and the rest arrived
  beside it), that sentence names the field and asks for the complete call again
  with each `old_string` and `new_string` inside its item: the parts are never
  joined, since which text belongs to which item is a guess. Text that ends
  before its JSON is complete asks for the complete call again, and for one edit
  per call if it is cut off again. A break inside a text (a quote, an escape, a
  line break) gets `If this text fails again, send one edit per call, or call
  write with the whole file.` A flat `old_string`/`new_string` call is one
  edit; an item's own `path` is accepted when it names the call's file (or
  supplies the missing root path) and refused when it names another file.
  Refused before any effect,
  each naming the call to send: `edits` beside flat fields, a root `replace_all`
  beside `edits`, `content` (points to `write`), no path, no change, a half pair.
  An `edits` item that is not an object, lacks `old_string` or `new_string`, has
  an unknown field with a value, gives a field twice with different values, or
  names another file is refused alone: the normalizer keeps it in place as a
  refused edit (an internal `_REFUSED` marker the Model never sees), the other
  edits keep their numbers and apply, and the result reports it as a failed
  edit with the refusal text (`Failed: edits item 3 has no old_string, ...`).
  When no item can apply, the call is refused naming every item's problem, one
  line each. An unknown item field with an empty value
  (`""`, `null`, `[]`, `{}`) requests nothing and is dropped, by the root rule
  of `ToolContract.normalize_arguments` applied to the item. A `new_string`
  without `old_string` says how to insert text with a neighbouring line; it adds
  that `old_string ""` creates a file unless another item of the call has old
  text, which shows that the file has text (the normalizer cannot see the file).
  Coverage:
  `test_other_edit_spellings_make_the_same_change`,
  `test_edits_json_text_runs_only_when_it_has_one_reading`,
  `test_open_or_misdirected_calls_say_which_call_to_send`.
- **Semantics:** the call is one step of the shared pipeline run with
  `one_write=True`: every edit is planned in memory, in order, each against the
  text the applied edits before it left, then the file is written once with the
  edits that applied. An edit that fails (not found, ambiguous, `line_join`, a
  `new_string` with a NUL character, a refused item) is skipped (`apply_hunks`
  with `failures`) and reported by its number; the others still apply (user
  decision 2026-10-12, replacing the former all-or-nothing call). After a failed
  edit, the later ones match precisely only (no `copy_match`): the text they
  expect can be text the failed edit would have written, so an edit that
  depends on a failed one fails to match instead of a guess. When no edit
  applies, or the applied ones leave the file as it was, nothing is written and
  the call fails. Problems of the whole call still write nothing: a missing or
  unreadable file, not a regular file, binary or not UTF-8 text, an empty
  `old_string` on a file with content (`file_exists`) or a creation with a NUL
  character, the write guard, a file changed during the call. Replay of the 432
  live `edit` calls since 2026-10-04 (2026-10-12): 9 of the 11 replayable
  multi-edit calls refused for `text_not_found` now apply in part (the other 2
  had every edit fail), and so do 5 recorded successes that the later
  `line_join` refusal refuses as a whole; in all 14, every applied edit wrote
  exactly its `old_string` -> `new_string` replacement (one through the
  `rewrapped` step, with its note), and the 395 recorded successes replay
  unchanged. Each `old_string` must match exactly one place (substring matching through the
  engine's precise steps, read gutters, then `copy_match`, never `copy_match` with
  `replace_all`; `apply_patch.md` -> Matching and recovery); several matches fail
  `ambiguous_match` with their lines, also after an earlier edit of the call.
  An `old_string` that differs from the file only in where its lines break (a
  signature wrapped after another argument, a paragraph sent as one line)
  matches through the precise `rewrapped` step (`apply_patch.md` -> Matching and
  recovery) when it has at least 4 words and the edit changes more than
  whitespace; `new_string` is written with its own line breaks, in the file's
  indentation and line endings, and a note names the matched lines. An edit of
  whitespace alone (the same text, or only a final line break more or less)
  stays `text_not_found`: replaying the two such failed calls with the step
  applied added or deleted a blank line the Agent's later fix did not have,
  because the Agents repaired a joined line without knowing where the file
  broke it.
  `replace_all` replaces every leftmost non-overlapping occurrence. An empty
  `old_string` creates a missing file (parent folders included) or fills an empty
  or whitespace-only one, and fails `file_exists` otherwise. Changing existing text
  needs no read stamp: the unique match against current bytes is the precondition;
  a file changed since the Session's read adds the stale note to a successful
  result or a match failure (`file_state.md`).
- **Results** are `apply_patch`'s (`_patch_report.patch_result`, `{content}`):
  `Updated X (places).`, with the changed regions as they are now only when the
  written text differs from the call or the call was partial (`apply_patch.md`),
  `Created X (N lines).`, notes and syntax warnings (checked on the written
  text); a note for identical old/new text, and
  `X already reads as this edit would leave it.` (`these edits`) when the edits
  leave the file as it was, such as a change and its reversal (the
  `already_applied` template).
  A call with failed and applied edits is a success led by the edit numbers in
  place of `apply_patch`'s change counts (`patch_result(..., lead=...)`,
  `_partial_lead`): `Edits 1, 2 and 4 were applied; edit 3 was not. Send only
  edit 3 again, corrected or left out.` (several failed: `... edits 2 and 3 were
  not. Send only edits 2 and 3 again, each corrected or left out.`; three or
  more in a row read `edits 1-3`). The file report with its changed regions
  follows, then one `Failed: ...` per failed edit in input order with the same
  report a failure of that edit gives alone. Each failed edit records an
  `error` notice, so the WebUI marks the call partial (`toolDisplayRecordsError`,
  as for `apply_patch`), and `_patch_report.edit_outcome` reads the lead as
  `partial`.
  An edit whose non-empty `new_string` is its `old_string` without the final
  line break changes nothing else; when the file continues after the match, the
  call is refused before any effect (`line_join`, `_edit_engine._refuse_join`,
  also for `apply_patch`'s `old_string` shapes): the edit would only join the
  next line to the matched text's last line, or delete the blank line after it.
  The refusal quotes both lines, at most 100 characters each, and says to leave
  the edit out or, to join the lines, to include the next line in `old_string`
  and `new_string`. In a call with other edits, such an edit is skipped and
  reported like any failed edit (Semantics). At the end of the file
  the edit applies: it only drops the final line break, and a `new_string` that
  still ends with a line break (`X\n\n` -> `X\n`) deletes a blank line as meant.
  Evidence: in the recorded Sessions (2026-10), 8 such items in 7 successful
  calls, mostly from one Model, joined code lines (twice breaking Python
  syntax) or deleted blank lines, none on purpose, and the Agents spent calls on
  reading and repairing them; 13 calls of the archived `edit` Tool (2026-08)
  show the same shape, and no follow-up shows a join that was meant (several
  call the edit unintended or a no-op, or repair the joined line).
  An edit that also changes text and drops the final line break joins the next
  line to its last line; when that line is not blank and not the end of the
  file, a note quotes the joined line, at most 100 characters on each side of
  the join (`_joined_lines`). That edit applies as sent: some joins are meant.
  Coverage: `test_edits_that_fail_are_skipped_and_the_others_written_once`
  (partial results, the single write, the display notices),
  `test_edits_that_leave_the_file_as_it_is_say_so`,
  `test_an_edit_that_drops_a_final_line_break_names_the_joined_line`,
  `test_old_string_with_line_breaks_in_other_places_than_the_file`. The
  display shows the path and the shared `file_changes` diff and notices.
- **Failures** use the engine's messages with `edit` wording where they name the
  Tool (`_EDIT_TEMPLATES`); every error first says what was applied, as the
  `vbot-agent-tools` Skill's Errors section requires (user decision 2026-10-12)
  (`_edit_failure`): one edit -> `No file was changed.` before its report;
  several -> each failed edit is labeled `edit K of N`, and when none applied the
  error opens with `Neither edit was applied; no file was changed.` (`None of the
  N edits were applied; ...`), lists every failed edit's report in input order
  (its code is the first one's) and closes with `Send both edits again, each
  corrected or left out.` (`all N edits`) when every edit failed, or `Send all N
  edits again with edit K corrected or left out.` when the edits that did not
  fail left the file as it was. A
  whole-call failure in a call with refused items lists them after its own
  message. A `text_not_found` after an earlier failed edit of the call adds
  `After an earlier edit of this call failed, old_string must match the file
  exactly (up to whitespace).` (`_PRECISE_AFTER_FAILURE`). A match report for an
  edit after applied ones shows the text those edits left, not the file, and says
  so where it names lines (`The closest text in the file after edits 1 and 2,
  ...`, `First difference: after edits 1 and 2, line N reads ...`, `Where it
  occurs after edit 1:`), naming only the earlier edits that applied; when shown
  lines are ones an earlier edit of the call wrote, it names that edit (`Edit 2
  of this call wrote line N.`; `apply_hunks` tracks each edit's written lines
  through the later ones). When an earlier edit of the call replaced the failed
  edit's `old_string` (the last edit whose text held it, matched precisely,
  `_edit_engine._replaced_by`), the report says so: in a partial result, where
  that edit stays written, `Edit 1 of this call already replaced this
  old_string; to change that text again, use what edit 1 wrote as old_string.`;
  when nothing was written, `...; merge the two edits into one.` (`... and wrote
  line N; ...` when that edit wrote a shown line). An `old_string` whose lines the file holds whole, each part at exactly
  one place, but at places apart (not in order with at most 3 non-blank lines
  left out between them) names the places instead of a closest text: `old_string
  joins lines from different places in the file: its lines 1-3 are lines 60-62
  and its lines 5-6 are lines 41-42. Give each place its own edit.` plus those
  lines (`_edit_engine._split_copy`; lines compare with whitespace collapsed).
  An `old_string` whose words the file holds at one place with other line
  breaks shows that place, and a first difference that is a line break says
  where the file and `old_string` break the line (`First difference, line N:
  the file breaks this line after '...'; old_string continues it with '...'.`,
  `apply_patch.md` -> Matching and recovery).
  Each part needs 2 lines with a word of 3 or more characters: in a replay of
  582 failed calls (2026-10), 4 of 8 such reports rested on single lines, one of
  them `return tool_success(result)` of another function; the first difference
  served those copies better. The other 4 each joined two functions or blocks the
  Agent took for neighbors. Quoted text in these reports keeps the file's
  characters (Agent-facing text -> Quoted text). Coverage:
  `test_failed_changes_change_nothing_and_say_what_to_send`,
  `test_a_file_changed_during_the_call_is_not_overwritten`,
  `test_reports_quote_text_as_the_file_has_it`.

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
| `Edit J of this call already replaced this old_string; merge the two edits into one.` | An item that reused the `old_string` of the item before it, which that item had replaced, got only `No similar text is in the file after edit 1` (Swarm, 2026-10-12), which does not say that the text was there before the call. Merging is the fix for both readings: the second change builds on the first, or the first was a draft of it. Now only when nothing was written. |
| `Edit J of this call already replaced this old_string; to change that text again, use what edit J wrote as old_string.` | In a partial result edit J stays written, so merging would resend an edit whose `old_string` is gone; the next call can only build on what edit J wrote. |
| Partial lead: `Edits 1, 2 and 4 were applied; edit 3 was not. Send only edit 3 again, corrected or left out.` (`edits 2 and 3 were not. Send only edits 2 and 3 again, each corrected or left out.`) | User decision 2026-10-12: a call applies every edit it can; results name the items by their numbers in input order (`vbot-agent-tools` -> Results). `Send only` keeps the Agent from resending the applied edits, whose `old_string` is gone or whose insertion would repeat; `left out` covers a failed edit whose change is already in the file. Replay 2026-10-12: 14 live calls refused as a whole apply in part this way. |
| `After an earlier edit of this call failed, old_string must match the file exactly (up to whitespace).` | After a failed edit the later ones match without copy tolerance, so a copy that applies in a call of its own fails here; without the sentence the not-found report reads as if the text were missing, and the Agent cannot tell why the same copy matched before. |
| `Neither edit was applied; no file was changed.` first, then the reports, then `Send both edits again, each corrected or left out.` (`None of the N edits were applied; ...`, `all N edits`) | When every edit failed, each has its report above; naming them all again in the closing adds nothing. |
| Split report: `old_string joins lines from different places in the file: its lines 1-3 are lines 60-62 and its lines 5-6 are lines 41-42. Give each place its own edit.` | An `old_string` that joined file lines 60-62 and 41-42 got the closest texts and a first difference at line 37, which named neither cause; the Agent rewrote the whole 142-line file with `write` instead (Swarm Run 2026-10). A passage that only left out a few lines keeps its first difference, which names the missing line. |
| Joined-line note: `new_string does not end with a line break as old_string does, so the next line now continues its last line: import a2 from 'a';import b from 'b';` | In the recorded Sessions (2026-10), 4 of 2751 edit items joined two non-empty lines this way, none on purpose; the result said only `Updated X (line N).`, since the written text was what the call sent. Three cost 2 to 3 more calls each to find and undo the join (reads, a failed edit, the fix); the fourth, a joined TypeScript import, stayed in the file. The quoted line is a ready `old_string` for the fix; no line number, because a later edit of the same call can move it. |
| `line_join`: `X, edit K of N: new_string is old_string without its final line break, so this edit would only join the line '...' with the next line, '...'. Leave this edit out; to join the two lines, include the next line in old_string and new_string.` | 8 items in 7 calls dropped only the final line break (Sessions, 2026-10); each joined code lines or deleted a blank line, none on purpose, and the result said only `Updated X (line N).`. The edit changes nothing else, so leaving it out is the usual fix; the second clause keeps an intended join possible without guessing. Quoting the lines instead of numbering them stays right when earlier edits of the call moved lines. |
| `blank_line_join`: `... so this edit would only delete the blank line after the line '...'. Leave this edit out; to delete that blank line, end old_string with two line breaks and new_string with one.` | The same shape before a blank line deletes it. "Include the next line" would invite `old_string` `X\n\n` with `new_string` `X`, which joins the line after the blank one; the counted line breaks name the edit that deletes only the blank line. |
| Rewrap note: `old_string matched lines 4-5, where the file breaks the text in other places; new_string was written with its own line breaks.` | 2 failed calls of one Swarm (2026-10-12) sent a signature wrapped after another argument and a Markdown list item as one line; every word matched. The note says the written line breaks are `new_string`'s, not the file's, so the Agent need not reread to see what the lines look like now. |
| Line-break first difference: `First difference, line N: the file breaks this line after '...'; old_string continues it with '...'.`, `... old_string breaks this line after '...'; the file continues it with '...'.`, `First difference: old_string starts a line with '...', which line N of the file has after '...'.` (after earlier edits: `after edits 1-2, line N breaks after ...`) | The former `the file has '<line>' where old_string has '<part of it>'` hid that only a line break differs; for an `old_string` starting with a line break inside a joined line, it named an unrelated line 9 lines above (Swarm, 2026-10-12). Quoting both sides of the break shows where to break or join `old_string`. |
| Quoted text in match reports, the `line_join` refusal and notes, such as `First difference, line 1: the file has "s = 'a°b'" where old_string has ...`: the characters as the file or the call has them, a backslash or quote single, an escape only for a character that shows nothing and is not whitespace (`​`) (`_patch_syntax.quoted`, shared with `apply_patch`) | `repr` doubled every backslash: a line holding `°` read `'...\\u00b0...'` in the first difference while the closest-text listing above it showed one backslash (Swarm review, 2026-10-12), and an Agent copying the quote into `old_string` would send two. Tabs and other whitespace stay as written, as in the listing: matching and the first difference compare runs of whitespace as equal, so a tab is never the difference shown, and a `\t` escape among literal backslashes could not be told from a file's own `\t`; the quotes show where trailing spaces end. A zero-width character keeps its escape: the first difference can rest on one, and shown raw both sides would read alike. |
| `edits item K has no old_string, the current text to replace. To insert text, put the line above it in old_string, and that line followed by the new text in new_string.` (`...; old_string "" creates a file.` unless another item has old text; flat: `new_string needs old_string, the current text it replaces. ...`) | Both such refusals in the live Sessions (Swarm, 2026-10-12) were inserts into a file other items of the call edited; the former `Send old_string and new_string (old_string "" creates a file)` named only file creation. Repeating the line in `new_string` keeps it, which Models miss when told only to use it as `old_string`. |
| `file_exists`: `...old_string is empty, which creates a file, but the file already has content. Put the current text to replace in old_string, or call write to replace the whole file.` | An empty `old_string` is the creation form; on an existing file both valid intents are named. |
| `file_not_found` for a page number: `w3.md is not a file. Swarm Wiki page w3 changes with swarm_wiki: action "update", page_id "w3", and old_text and new_text for the passage.` | An `edit` call named a Wiki page number as `path` (Swarm Sessions, 2026-10-12). No ready call: the Wiki takes its own `old_text` and `new_text`. Only when `swarm_wiki` is offered; built with `missing_file_message` (`tools/read.md`), shared with `apply_patch`. |
| Broken `edits` JSON text: `edits is not valid JSON. At character N, <what breaks in which field>:` + the text around it + how to write it | The former `edits arrived as text, not as an array.` named no break, so the Model could not tell what to fix; 12 `edits` texts in the live Sessions failed this way, 7 of them still after the quote repair (tool review 2026-10-11). |
| `If this text fails again, send one edit per call, or call write with the whole file.` | Only for a break inside a text (a quote, an escape, a line break) with no edit field beside `edits`: long code with quotes is what Models fail to escape; a smaller call or `write` avoids the nested JSON. Dispatch does not know whether the call repeats, so the sentence is conditional. |
| `old_string arrived beside edits instead of inside an edits item. Send the complete call again, with each old_string and new_string inside its edits item.` (`new_string` or `old_string and new_string` when those arrived) | In the Swarm of 2026-10-12 one `edits` text ended after part of its item while its `old_string` arrived as a root field, and another had an unquoted `new_string` with `old_string` beside it. The former result named only the break, which reads as a typo to fix in place; the call came apart, and joining the parts would guess which text belongs to which item. |
| `Send the complete call again; if it is cut off again, send one edit per call.` | For text that ends before its JSON is complete and has nothing beside it. `the JSON ends before it is complete` alone does not say that the rest of the call was lost, and fixing the shown end cannot restore it. A cut that repeats is usually a length limit, which smaller calls avoid. |

## Verification

- `tests/core/tools/test_edit_tools.py`: activation and pinned definitions, the
  dialect table, ordered edits written once, failed edits skipped (partial and
  none applied), creation, spellings, the full failure and refusal inventories,
  unknown parameters, a race during the call.
- `tests/core/chat/test_chat_loop_tool_definitions.py` (offered dialect per family,
  epoch pin across a Model change), `test_tool_dispatch.py` (one permission),
  `test_model_names.py` (harness names), `test_read_fields.py`,
  `test_search_files_arguments.py` and `test_shell.py` (texts naming the offered
  edit Tool).
- `scripts/tool_lab/cases/files.json` holds edit and write cases for
  `python -m scripts.tool_lab probe`.
