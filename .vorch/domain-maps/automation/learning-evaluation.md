# Learning Evaluation

Read when running, extending or interpreting the learning evaluation of Reflection reviews, `/learn` and Librarian passes: cases, scoring, text packs, reports. Review orchestration itself is in `../automation.md` -> Reflection, the Librarian in `../automation.md` -> Librarian.

## Owner and entry points

- `scripts/provider_probe/workflow_reflection.py` runs the `reflection_workflow` probe scenario: selection, repetitions, workers, the attempt loop.
- `learning_fixture.py` (`EvalWorker`) owns the disposable vBot fixture, prompt rendering and dispatch; `learning_scoring.py` the programmatic scoring; `learning_texts.py` the learning texts, text packs and brief assembly (`brief_text`); `learning_eval.py` reports, `export-pack` and `compare`.
- The smoke test is `test_learning_evaluation_runs_a_text_pack_arm_and_compares_reports` in `tests/scripts/test_provider_probe.py` (scripted adapter, no Provider).
- Keep packs and reports outside the repository, by convention under `~/.cache/vbot-evals/learning/` (`packs/`, `reports/`).

## Evaluation rules

- Run at least three repetitions per (case, scope); weak Models carry the signal. `--reflection-case` (comma-separated ids) and `--reflection-scope` (one or more of `memory`, `skill`, `combined`, `learn`, `librarian`, or `all`) narrow the matrix.
- Run the arms on `--provider opencode-go --model deepseek-v4.1-flash` and `--provider ollama-cloud --connection ollama-cloud:api-key --model glm-5.3-flash` (user decision 2026-10-02: on opencode-go only that Model; `--connection` defaults to `opencode-go:api-key`).
- Compare arms only with equal attempt counts per (case, scope); never drop an attempt from one arm. A crashed attempt stays in the report with its error and counts as a failure.
- Scoring is code only, no LLM judge. Every attempt is a Provider request loop: do not run the full matrix casually.

## Fidelity to production

- **Fixture:** each worker owns one Runtime on a temporary data directory, started like the other probes (`_start_probe_runtime`). The Identity Agent `main` gets the probe's Model and thinking effort, so the Runtime Environment block names them. Before every attempt the worker restores `<data>/agents/main/` (with the Agent's Skill history and archive), `<data>/skills/`, `<data>/skill-history.jsonl` and `<data>/skill-archive/` to their post-start files and invalidates the Skill registries.
- **Seeding:** Memory entries go through `MemoryService.add_entry` (budgets apply); own Skills through `SkillAuthoringService.create` into the Agent's Skill home with the case `origin` as writer (`HUMAN_WRITER`, or `SkillWriter(actor=origin)` for `agent`, `reflection` and `librarian`), so the Skill history records that origin; a `pinned` Skill is then pinned by a person (`set_pinned`); `readonly` Skills go into the global Skill home.
- **Prompt:** production `build_system_prompt` with the Session pins: Memory from `render_memory_files` (rendered before seeding for `stale_memory_prompt`, like a pin older than the stored entries), the Skill catalog, SOUL.
- **Tools:** the Agent's full provider definitions, so the review sees what the reviewed Session saw. The route projection is approximated: production bash projection at nesting depth 0 and no `analyze_image`, which production drops while no image-understanding Model task is configured (`TOOL_ROUTE_NOTE`). The scope restriction applies only at dispatch (`_dispatch_allowed_tools`, `ToolExecutor`); a review answers an out-of-scope call with production's denial resolver (`_review_tool_denial_resolver`, `REVIEW_TOOL_DENIAL_MESSAGE`) before anything runs, and a Librarian pass narrows dispatch to the Librarian's Tools (`core.agents.LIBRARIAN_TOOLS`) with the executor's generic `tool_not_allowed` answer. Calls carry the production Run kind (`REFLECTION_RUN_KINDS[scope]` for a review, `librarian` for a pass, `user` for `/learn` and for `dispatch: true` history calls), so `skill_manage`'s background guards apply: a review's or pass's write to a pinned Skill fails with `skill_protected` and changes nothing.
- **Messages:** case history becomes canonical `ChatMessage`s projected by `_prepare_request_messages`; the brief is a note, rendered as a System Reminder after the history. `brief_text` calls production `reflection_brief` (no focus, as the cadence trigger starts a review), `learn_brief` with the case request, or `librarian_brief` naming the Agent `main` as the one whose Skills the pass maintains, reading the run's fragment texts in place of Storage. The Librarian brief lists production `librarian_candidates` of the seeded home; the fixture has no Skill use, so every candidate reads as never used. A pass starts a new Session, so Librarian cases have an empty `history`. A history Tool message with `dispatch: true` gets its result by dispatching the preceding call, which also registers Skill activations for the attempt.
- **Loop:** non-streaming `send` with production temperature/top_p resolution and request-context kwargs. Tool use ends like a Chat Run (`_ToolBudget`): calls beyond `REFLECTION_TOOL_ITERATION_LIMIT` (Chat's `MAX_TOOL_ITERATIONS` for a pass, the limit of every Agent's Runs) dispatched Tool iterations, or an identical failed call repeated `MAX_IDENTICAL_FAILED_TOOL_CALLS` times (Chat's failed-call breaker), get Chat's failure results and finalization note; later requests still offer the same Tools, calls fail unrun, and the second such round ends the attempt (`tool_calls_after_finalization`). An attempt also ends at a final answer or at `--total-timeout`.
- **Deviations:** `/learn` dispatch is narrowed to `memory`, `skill` and `skill_manage` (production allows every Tool, refused with the generic `tool_not_allowed` result here) so an evaluation never runs commands or reaches the network, and `/learn` stops after 30 Tool iterations (`LEARN_TOOL_ITERATION_LIMIT`) instead of Chat's loop limit. The finalization failure texts are imported from `core/chat/_step_outcomes.py`. The `librarian` scope evaluates only a pass's consolidation Run: aging archives inactive Skills without a Model and is covered by `tests/core/automation/test_librarian.py`, and the harness sends the brief even where production would skip consolidation (fewer than two candidates, or no candidate changed since the last merge). A pass runs as the evaluated Agent `main` with its System Prompt and full Tool definitions, while production runs it as the built-in Librarian Agent, whose own System Prompt and definitions offer only `skill` and `skill_manage`, in a Session bound to the Agent (`automation/librarian.md`); the Skills it reads and changes are the same.
- **Private production names:** the harness imports `_prepare_request_messages`, `_dispatch_allowed_tools`, `_review_tool_denial_resolver` and `core/chat/_step_outcomes.py` names; renaming them breaks evaluations, which the smoke test catches.

## Cases

`tests/fixtures/reflection/cases.json`, one object per case. Only `history`, `memory`, `skills` and `learn_request` reach the Model, and they carry no hints about the expected outcome.

- `history`: `user` and `assistant` messages; assistant `tool_calls` (`id`, `name`, `arguments`, registry names); `tool` messages with `tool_call_id`, `name` and either a result envelope in `result` or `dispatch: true`.
- `memory`: `user`/`agent` entry lists. `stale_memory_prompt: true` pins empty Memory in the prompt.
- `skills`: `name`, `content` (complete SKILL.md), `readonly` (global, not writable by `skill_manage`), `origin` `agent` (default), `human`, `reflection` or `librarian`, `pinned`. A pinned own Skill must not be changed by a review or a pass; since 2026-10-03 a human-origin one can be (`skills.md` -> Background Writer).
- `learn_request` (cases with a `learn` scope), observer-only `note` (copied into the report), `preference_tokens`, `forbidden_claims`, `name_excludes`.
- `expected`: per scope (`memory`, `skill`, `combined`, `learn`, `librarian`) one outcome or `{"any_of": [...]}`. Kinds: `none` (no write attempted, nothing changed), `user`/`agent` (only that Memory scope changed and holds `count` entries, default 1), `create` (exactly one new own Skill, Memory unchanged), `update` (only the own Skill `name`, or one of `names`, changed), `consolidate` (the `cluster` Skills merged into one umbrella: the one cluster Skill left or one new own Skill, optionally one of `umbrellas`; every other cluster Skill deleted with `absorbed_into` naming it; nothing else changed). `contains`/`excludes` check the written text case-insensitively (for `consolidate`, every file of the umbrella), `reply_contains` the final answer.
- Librarian cases: `librarian_overlapping_cluster` (three narrow deploy Skills, one rule each to keep), `librarian_protected_cluster` (an overlapping group whose other members are human-origin or pinned: nothing to merge; an expectation from before 2026-10-03, see Results), `librarian_healthy_library` (no change, "Nothing to change.").

## Scoring

An attempt passes when the Model finished with a final answer, its effect matched one acceptable outcome (`effect_passed`) and no violation occurred:

- `memory_write_without_current_list`, `create_without_current_catalog`, `skill_write_without_current_file`: the write was not based on a current read in this attempt. A Memory change without `scope` needs both scopes listed; a `patch` of SKILL.md and a `delete` also count a plain load of the Skill (status `loaded`) as their read; a Librarian pass starts in a new Session whose System Prompt lists the current catalog, so its `create` needs no catalog call. Calls of one Model turn are checked before any of them runs.
- `tool_call_rejected`: any failed result of a dispatched call, out-of-scope denials included.
- `tool_iteration_limit`, `repeated_failed_call`: Tool use ended by the iteration limit or the failed-call breaker.
- `protected_skill_write`: any `skill_manage` call naming a read-only or pinned Skill in `name` or `absorbed_into`, whatever the result. Production refuses such a background write (`skill_protected`), so it usually also counts as `tool_call_rejected`.
- `delete_without_absorbed_into`: in the `librarian` scope, a `skill_manage` delete without `absorbed_into`.
- `ticket_like_skill_name`: a created Skill name with digits, a ticket/jira/incident/hotfix segment, or a case `name_excludes` token.
- `duplicate_preference`: a `preference_tokens` entry newly present in both Memory and an own Skill.
- `forbidden_claim`: a written sentence matching a `forbidden_claims` regex; sentences with if/when/whenever/unless/in case state a condition and are skipped.

## Text packs

`export-pack DIR` writes the checkout's current learning texts: `blocks/{memory_guidance,skills,skill_maintenance}.md`, `tools/{memory,skill,skill_manage}/description.md`, `tools/<name>/parameters.json` (JSON-pointer path to parameter description), `fragments/<name>` for every name in `core.prompts.briefs.BRIEF_FRAGMENT_NAMES` (the set follows the checkout; it includes the Librarian brief `librarian.md`), and `manifest.json` (format 2, commit, text digests). Production joins the fragments into each brief, so a pack changes brief wording, never brief composition.

`--text-pack DIR` replaces exactly these texts: block overrides through `update_block` in each fixture, Tool and parameter descriptions in the offered definitions, fragments in brief assembly. Block and Tool files must exist; an older pack format is refused. A text equal to the current one apart from surrounding whitespace keeps the current text, so a baseline pack reproduces production byte for byte. A parameter path or fragment the checkout does not use is ignored, a current path or fragment missing from the pack keeps production text, and a block lacking a `{generated:...}` marker of its current text loses that content; each case warns on stderr and in the report. After fragments are renamed, re-export the baseline and port candidate edits.

## Reports

`--reflection-report PATH` is rewritten after every attempt. Top level: `format`, `kind: learning_evaluation`, `run` (Provider, Model, thinking effort, repetitions, pairs, case notes, commit, `text_pack` with changed text ids and warnings, effective text digests, tool route note), `summary`, `pass_rates` per (case, scope), `attempts`, and the distinct `system_prompts` and `definitions` by digest. Each attempt has case, scope, repetition, `passed`, `effect_passed`, `effect` detail, `violations`, `finished`, `stopped_reason` (`final_answer`, a terminal outcome, or `tool_calls_after_finalization`), `error`, steps, Tool iterations, summed `usage`, final text, Memory/Skill `state` before and after, and the transcript (request messages after the System Prompt plus every call with arguments and result). Stdout gets a compact result without transcripts; stderr a per-attempt line and the pass-rate table.

`compare A B` recomputes pass rates from the attempts, prints per (case, scope) `passed/attempts` and the delta for equal attempt counts, flags other cells as not comparable, lists the texts whose digests differ and the overall delta over comparable cells; `--json` prints the comparison as JSON.

## Results

Skill-first learning texts (`learning-texts`, 2026-10-02) against the texts at `3edacbd2b`, 3 repetitions of all 58 (case, scope) pairs, both arms rescored with the current scorer:

| Model | Baseline | Candidate |
|---|---|---|
| `opencode-go/deepseek-v4.1-flash` | 140/174 | 167/174 |
| `ollama-cloud/glm-5.3-flash` | 137/174 | 153/174 |

Writes without a current Memory list fell from 19 and 27 to 1 and 5, duplicated preferences from 7 to 0. A focused rerun after the last text fix (6 Skill cases) met every expected effect except `human_skill_wrong` on glm-5.3-flash (1 of 6). Known weakness: glm-5.3-flash patches its own Skills from the conversation without reading the current file in about a third of Skill writes (14 of 45 attempts in the focused rerun); the patches had the expected effect, and writes to protected Skills are refused by the handler. If real reviews show collateral loss from such patches, enforce read-before-write for background Runs in `skill_manage`.

Librarian brief (`librarian`, 2026-10-02), 3 repetitions of the three `librarian` cases: `opencode-go/deepseek-v4.1-flash` 9/9, `ollama-cloud/glm-5.3-flash` 8/9 (every expected effect met). glm-5.3-flash omitted `absorbed_into` on 3 deletes and repeated them correctly after the refusal.

Not yet rerun: these results predate the built-in Librarian Agent and the removal of the background guards on human-origin, shared and automation-triggered Skills (2026-10-03). Two cases still expect the old guards and need new expectations before the next run: `human_skill_wrong` (expects no change to a human-origin Skill a review may now correct) and `librarian_protected_cluster` (its human-origin member is now a candidate, so only the pinned member keeps the group from merging whole).
