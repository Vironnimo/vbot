# Learning Evaluation

Read when running, extending or interpreting the learning evaluation of Reflection reviews and `/learn`: cases, scoring, text packs, reports. Review orchestration itself is in `../automation.md` -> Reflection.

## Owner and entry points

- `scripts/provider_probe/workflow_reflection.py` runs the `reflection_workflow` probe scenario: selection, repetitions, workers, the attempt loop.
- `learning_fixture.py` (`EvalWorker`) owns the disposable vBot fixture, prompt rendering and dispatch; `learning_scoring.py` the programmatic scoring; `learning_texts.py` the learning texts, text packs and the brief seam `brief_text`; `learning_eval.py` reports, `export-pack` and `compare`.
- The smoke test is `test_learning_evaluation_runs_a_text_pack_arm_and_compares_reports` in `tests/scripts/test_provider_probe.py` (scripted adapter, no Provider).
- Keep packs and reports outside the repository, by convention under `~/.cache/vbot-evals/learning/` (`packs/`, `reports/`).

## Evaluation rules

- Run at least three repetitions per (case, scope); weak Models carry the signal.
- Compare arms only with equal attempt counts per (case, scope); never drop an attempt from one arm. A crashed attempt stays in the report with its error and counts as a failure.
- Scoring is code only, no LLM judge. Every attempt is a Provider request loop: do not run the full matrix casually.

## Fidelity to production

- **Fixture:** each worker owns one Runtime on a temporary data directory, started like the other probes (`_start_probe_runtime`). The Identity Agent `main` gets the probe's Model and thinking effort, so the Runtime Environment block names them. Before every attempt the worker restores `<data>/agents/main/` and `<data>/skills/` to their post-start files and invalidates the Skill registries.
- **Seeding:** Memory entries go through `MemoryService.add_entry` (budgets apply); own Skills through `SkillAuthoringService.create` into the Agent's Skill home with `author` from the case `origin`; `readonly` Skills into the global Skill home.
- **Prompt:** production `build_system_prompt` with the Session pins: Memory from `render_memory_files` (rendered before seeding for `stale_memory_prompt`, like a pin older than the stored entries), the Skill catalog, SOUL.
- **Tools:** the Agent's full provider definitions, so the review sees what the reviewed Session saw. The route projection is approximated: production bash projection at nesting depth 0 and no `analyze_image`, which production drops while no image-understanding Model task is configured (`TOOL_ROUTE_NOTE`). The scope restriction applies only at dispatch (`_dispatch_allowed_tools`, `ToolExecutor`), so a denied call gets production's result.
- **Messages:** case history becomes canonical `ChatMessage`s projected by `_prepare_request_messages`; the brief is a note, rendered as a System Reminder after the history. A history Tool message with `dispatch: true` gets its result by dispatching the preceding call, which also registers Skill activations for the attempt.
- **Loop:** non-streaming `send` with production temperature/top_p resolution and request-context kwargs. An attempt ends at a final answer, when the Model requests Tool iteration 17 of a review (`REVIEW_TOOL_ITERATION_LIMIT`, mirroring the planned review Run limit) or 31 of `/learn` (`LEARN_TOOL_ITERATION_LIMIT`), or at `--total-timeout`.
- **Deviations:** `/learn` dispatch is narrowed to `memory`, `skill` and `skill_manage` (production allows every Tool) so an evaluation never runs commands or reaches the network. The brief seam reads prompt fragments as production does today; when review or `/learn` brief assembly changes, `brief_text` changes with it.

## Cases

`tests/fixtures/reflection/cases.json`, one object per case. Only `history`, `memory`, `skills` and `learn_request` reach the Model, and they carry no hints about the expected outcome.

- `history`: `user` and `assistant` messages; assistant `tool_calls` (`id`, `name`, `arguments`, registry names); `tool` messages with `tool_call_id`, `name` and either a result envelope in `result` or `dispatch: true`.
- `memory`: `user`/`agent` entry lists. `stale_memory_prompt: true` pins empty Memory in the prompt.
- `skills`: `name`, `content` (complete SKILL.md), `readonly` (global, not writable by `skill_manage`), `origin` `agent` (default) or `human`. A human-origin own Skill must not be changed by a review.
- `learn_request` (cases with a `learn` scope), observer-only `note` (copied into the report), `preference_tokens`, `forbidden_claims`, `name_excludes`.
- `expected`: per scope (`memory`, `skill`, `combined`, `learn`) one outcome or `{"any_of": [...]}`. Kinds: `none` (no write attempted, nothing changed), `user`/`agent` (only that Memory scope changed and holds `count` entries, default 1), `create` (exactly one new own Skill, Memory unchanged), `update` (only the own Skill `name`, or one of `names`, changed). `contains`/`excludes` check the written text case-insensitively.

## Scoring

An attempt passes when the Model finished with a final answer, its effect matched one acceptable outcome (`effect_passed`) and no violation occurred:

- `memory_write_without_current_list`, `create_without_current_catalog`, `skill_write_without_current_file`: the write was not based on a current read in this attempt. Calls of one Model turn are checked before any of them runs.
- `tool_call_rejected`: any failed Tool result.
- `protected_skill_write`: any `skill_manage` call naming a read-only or human-origin Skill, whatever the result.
- `ticket_like_skill_name`: a created Skill name with digits, a ticket/jira/incident/hotfix segment, or a case `name_excludes` token.
- `duplicate_preference`: a `preference_tokens` entry newly present in both Memory and an own Skill.
- `forbidden_claim`: a written sentence matching a `forbidden_claims` regex; sentences with if/when/whenever/unless/in case state a condition and are skipped.

## Text packs

`export-pack DIR` writes the checkout's current learning texts: `blocks/{memory_guidance,skills,skill_maintenance}.md`, `tools/{memory,skill,skill_manage}/description.md`, `tools/<name>/parameters.json` (JSON-pointer path to parameter description), `briefs/{memory,skill,combined,learn}.md` and `manifest.json` (format, commit, text digests). `briefs/learn.md` is the `/learn` base; the harness appends each case's request like production.

`--text-pack DIR` replaces exactly these texts: block overrides through `update_block` in each fixture, Tool and parameter descriptions in the offered definitions, briefs in the seam. All files must exist. A text equal to the current one apart from surrounding whitespace keeps the current text, so a baseline pack reproduces production byte for byte. A parameter path missing from the current schema is ignored, a current path missing from the pack keeps production text, and a block lacking a `{generated:...}` marker of its current text loses that content; each case warns on stderr and in the report.

## Reports

`--reflection-report PATH` is rewritten after every attempt. Top level: `format`, `kind: learning_evaluation`, `run` (Provider, Model, thinking effort, repetitions, pairs, case notes, commit, `text_pack` with changed text ids and warnings, effective text digests, tool route note), `summary`, `pass_rates` per (case, scope), `attempts`, and the distinct `system_prompts` and `definitions` by digest. Each attempt has case, scope, repetition, `passed`, `effect_passed`, `effect` detail, `violations`, `finished`, `stopped_reason`, `error`, steps, Tool iterations, summed `usage`, final text, Memory/Skill `state` before and after, and the transcript (request messages after the System Prompt plus every call with arguments and result). Stdout gets a compact result without transcripts; stderr a per-attempt line and the pass-rate table.

`compare A B` recomputes pass rates from the attempts, prints per (case, scope) `passed/attempts` and the delta for equal attempt counts, flags other cells as not comparable, lists the texts whose digests differ and the overall delta over comparable cells; `--json` prints the comparison as JSON.
