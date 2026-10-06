# Decision Model

`core/model_tasks/decisions.py::DecisionService` is the one stateless executor of Decision Model requests: typed answers to questions about text. It extends Task Models because every consumer needs the same configured `decision` binding, validation, Provider execution and Usage. `decision_types.py` (validation) and `decision_providers.py` (wire) are internal parts of this owner. It keeps no records: there is no decision database, experiment, history or control loop (all removed 2026-10-06, user decision: only Tools remain).

## Interfaces

- Task binding: `decision`, currently OpenRouter API-key targets with `decisions` output (`typesafe/jev-1.13`, `~typesafe/jev-latest`). Discovery includes `GET /api/v1/models?output_modalities=decisions`; a Decision Model is not a Chat Model. Settings shows it under Tools -> Decision Model.
- `available()`: the binding is usable and its target is an OpenRouter target, the only implemented wire. Tools use it as readiness.
- `classify(items, questions, *, context=None, usage_context=None) -> list[dict | DecisionError]`: answers every question about each item separately, in item order. Each item is one request whose state is the item itself, or `{"context": context, "item": item}` when `context` is given. Consumers: the `classify` Tool (`../tools/classify.md`); future specialized Tools (for example one that picks the relevant files for a task) build their items and questions and call the same method instead of a second executor.
- Provider wire: `POST /api/alpha/decisions` at the configured OpenRouter origin, using the existing Connection credential and shared `ProviderTaskClient` retry/auth classification. Ambiguous outcomes are never automatically replayed.

## Judgments

State is text, a JSON object or a JSON array, preserved without interpreting embedded field names. Questions have unique ids, a type, complete instructions, and type-specific criteria. The wire vocabulary is `noul` (probability of yes, 0 to 1; optional `true`/`false` criteria), `choice` (2 to 255 label-to-description entries, labels at most 128 characters) and `score` (2 to 10 level descriptions, answered by level number from 0, possibly interpolated). Tools may present other names (the `classify` Tool says `yes_no`, `options`, `levels`) and convert them before calling. Images and generated explanations are unsupported.

Responses validate matching ids, types, labels, finite ranges, supplied distributions and token usage. Optional probabilities, confidence and cost remain absent if the Provider omits them. Confidence describes distribution concentration, not accuracy. Live September 2026 probes found uncertain noul answers for empty objects even when the question requested explicit evidence; exact missing-field checks belong in code, not in a judgment. TypeSafe documents that the model cannot count, calculate or compare dates. Reference: [TypeSafe primitives](https://docs.typesafe.ai/primitives).

## Execution

- Invalid questions or items, more than `ITEM_LIMIT` (100) items, empty `context` and a missing binding raise `DecisionError` before any request is sent.
- An item whose estimated state-plus-questions tokens exceed `INPUT_TOKEN_LIMIT` (32,000, the model's limit) fails alone with code `too_large` and sends nothing.
- At most 8 items run at once over one shared HTTP connection pool, closed when the call ends.
- A failed item yields its `DecisionError` in its place; the other items still complete. Codes: `invalid_request`, `not_configured`, `too_large`, `provider_error`, `outcome_unknown` (the Provider may have processed and billed the request; asking again may incur another charge), `invalid_result`.
- Every Provider attempt writes canonical `decision` Usage with the caller's `TaskUsageContext` (Run/Session/Project and Extension scope); optional Provider counters and cost remain unknown when absent.

## Retired database

The former `decisions` database (`<data-dir>/decisions.db`) is listed in `core/runtime/databases.RETIRED_CANONICAL_DATABASES`. A normal Runtime start quarantines and unregisters it through `retire_core_databases` (`../database.md`); an update's verification start does not, because its rollback needs every registration unchanged. Keep the entry while installations may still carry the file.

## Verification owners

- `tests/core/model_tasks/test_decisions.py`: one bounded request per item in item order, partial failure, oversized items, nothing sent for invalid calls or a missing binding.
- `tests/core/model_tasks/test_decision_{types,providers}.py`: validation and the wire (exact request, auth, no ambiguous replay, shared connection).
- `tests/core/model_tasks/test_task_usage.py`: `decision` Usage per request.
- `tests/core/runtime/test_runtime_lifecycle.py::test_only_a_normal_start_releases_retired_core_databases`.
