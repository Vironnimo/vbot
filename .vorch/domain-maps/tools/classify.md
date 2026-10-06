# Classify Tool

`core/tools/classify.py` registers the flat execution-family Tool `classify(items, questions, context?)` against the Runtime's shared DecisionService; `core/tools/_classify_arguments.py` reads the calls. It returns typed answers from the configured Decision Model for each item, without starting an Agent Run. The executor contract (limits, per-item requests, error codes, Usage) is `../model_tasks/decisions.md`.

## Interfaces

- Schema: `items` (array of strings, at most 100) and `questions` are required; `context` is optional text every item is judged against. Each question has `type` (`yes_no|choice|score`) and `instructions` (required), an optional `id` (defaults to `q1`, `q2`, ... by position, skipping ids already taken), `options` for choice (label -> meaning, at least two) and `levels` for score (meanings, lowest first, answered by level number from 0). Separate `options` and `levels` fields replace one `criteria` union so the schema stays flat. The handler converts to the wire form (`yes_no` -> `noul`, `options`/`levels` -> `criteria`).
- Results: `content` text, one line per item in item order. With several items each line starts `item <n> (<first line, at most 50 characters>): `; with several questions each answer starts `<id>: ` and answers join with `; `. Answers: `probability of yes <p>`, `<label>` (`<n> '<text>'` for options numbered `1`..`n`), `level <n> on levels 0-<max>`, each followed by `(confidence <c>; <label> <p>, ...)` when the Provider reported them. Numbers show three significant digits; missing probabilities and confidence are not invented.
- Partial failure: a failed item reads `not classified: <message>`, and a final line names the failed item numbers and says to repeat the call with only those items. When every item failed, the call fails with the first item's code and message (prefixed `No item was classified. ` for several items). Whole-call failures (`not_configured`, `invalid_request`) carry the DecisionError code and message.

## Argument Reading

The argument normalizer (no context) maps other shapes onto the canonical call: one text as `state`/`text`/`input`... becomes the only item, or the context when `items` is also given (a third, different text is refused); `item`/`texts`/`files`/`documents`... -> `items`; `task`/`goal`/`query`... -> `context`; a single item string, a JSON-array text or one object becomes the item list, and numbers, booleans, objects and arrays are judged as their JSON text. Questions: the wire's map of id to question, one question object, bare question text, a JSON-encoded list, and question fields written straight into the call (merged into the only question). Per question `name`/`key` -> `id`, `kind` -> `type`, `question`/`prompt`/`text` -> `instructions`, `choices`/`labels` -> `options`, `scale`/`rubric` -> `levels`; `options`, `levels` or `criteria` move to the field of the question's type. Type words: `noul`/`boolean`/`probability`... -> `yes_no`, `classification`/`select`/`multiple_choice`... -> `choice`, `rating`/`scale`/`likert`... -> `score`. Choice options given as a label list use each label as its own meaning (label objects take `label`/`name` plus `description`/`meaning`); a list of texts with one longer than 128 characters is numbered instead, labels `1`..`n` in list order (evidence: one Agent sent four option sentences, got the unexplained label-length error twice and dropped the question, Sessions 2026-09); score levels numbered `0..n-1` become the level list; `yes`/`no` keys become the hidden yes_no `true`/`false` criteria.

It never infers a missing type, never matches type typos, and never shifts score levels. Refusals start with `classify was not run:`, happen before the Provider is called, and show the corrected question: a missing type offers the yes_no, choice and score forms; score levels numbered from another start show the list with the note which level becomes 0; yes_no criteria other than exact `true`/`false` say to describe yes and no in the instructions; unsupported question fields (such as `explain`) are named with the question without them; a field given inside and outside the question with different values, or as both `options` and `levels` with different values, offers the question to send; loose fields beside several questions are refused. Missing `items`/`questions` and duplicate ids are refused with the missing piece. `decision_types.validate_questions` stays the final guard.

## Conventions

The Tool is registered consistently but only exposed when the Decision binding is usable through the implemented OpenRouter wire; dispatch checks readiness before reading arguments, so the `tool_lab` probe (no Decision model) reaches only the readiness hint. Tool Access Policy applies. The display summary counts the items after argument reading. No Skill is needed for this self-contained operation.

Tests: `tests/core/tools/test_classify.py` (registry dispatch: item preservation, context per item, readiness, Usage attribution, result lines, partial failure, other argument shapes, refusals) and the executor tests listed in the canonical map. They verify execution contracts, not independent Model Tool choice; black-box evidence follows the `tool-review` Skill.

## Agent-facing text

| Text | Reason |
|---|---|
| `Classify texts with a separate, fast decision model.` | Names the job and that a different model answers, so Agents do not expect their own reasoning or a Chat reply. |
| `For each item it answers your questions with a probability of yes, one of your options, or a level on your scale.` | States the three answer forms and per-item fan-out before the schema is read, so Agents do not expect free-text answers. |
| `Use it to filter, sort or label many texts, or to get a probability instead of judging yourself.` | Tool choice: names the situations where the Tool beats judging the texts in the Agent's own context. |
| `It sees only what you pass, gives no reasons, and cannot count, calculate or compare dates.` | Prevents calls that rely on conversation context, request explanations (refused fields), or ask arithmetic and date questions the model answers unreliably (TypeSafe docs). |
| `items`: `Texts to classify, such as one file path with its first lines per item.` | Shows that an item is self-contained text, with the motivating file-selection use. |
| `items`: `Each item is judged on its own with every question.` | Prevents questions that compare items or reference "the list". |
| `context`: `Text every item is judged against, such as the task or the user's request.` | Lets Agents state the task once instead of repeating it inside every item or question. |
| `context`: `Omit when the questions say everything needed.` | Weak Models fill every optional field. |
| `questions`: `Questions asked about every item.` | Pins down that one question set applies to all items. |
| `id`: `Name of this answer in the result. Omit to use q1, q2, ...` | Ids are only labels; omission is common and handled. |
| `type`: `yes_no: probability of yes, 0 to 1. choice: ... score: a level number from levels, starting at 0.` | Answer form and the 0-based level base, which Agents could otherwise assume to be 1-based. |
| `instructions`: `The complete question about one item.` | Prevents fragments that rely on context the model does not see. |
| `options`: `Required for choice: each label mapped to what it means, e.g. {...}. Include a catch-all label when no option might fit.` | Exact map shape; a missing catch-all forces a wrong label. |
| `levels`: `Required for score: what each level means, lowest first; 2 to 10.` | Order and the model's level count limit. |
