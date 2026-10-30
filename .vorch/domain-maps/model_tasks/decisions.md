# Decision Model

`core/model_tasks/decisions.py::DecisionService` is the one stateless executor of native Decision requests: typed answers to questions about text. Task Models own its binding, discovery, validation and Usage; `decision_profile.py` combines the selected wire with published Model facts, `decision_types.py` validates judgments, and `decision_providers.py` translates the native protocols. It keeps no records: there is no decision database, experiment, history or control loop (removed 2026-10-06, user decision: only Tools remain).

## Interfaces

- Task binding: `decision`. Selection and validation require the Model's explicit `decision` task tag, a usable allowed Connection and a supported native route from its resolved `WireProfile.decision`. No Provider or Model-name allowlist applies. A decisions-only Model is not a Chat Model; a Model such as OpenAI GPT-6 Luna can expose both tasks, with separate contracts. `typesafe/jev-router` is a Chat router without the Decision tag. Settings presents the binding under Tools -> Decision Model.
- `TaskModelService.decision_profile(ref)` is the shared target contract for selection, Settings and execution. Runtime injects its live wire-profile resolver; standalone callers use bundled profiles. Model-specific route corrections belong in the wire file, published limits and prices in `capabilities.task_options.decision`. A missing or unsupported stored target remains stored but unavailable, with `not_configured` at execution.
- `available()` checks that exact native binding, including live Connection/Account availability; Tools use it as readiness.
- `classify(items, questions, *, context=None, usage_context=None) -> list[dict | DecisionError]`: answers every question about each item separately, in item order. Each item is one request whose state is the item itself, or `{"context": context, "item": item}` when `context` is given. Consumers: the `classify` Tool (`../tools/classify.md`); future specialized Tools (for example one that picks the relevant files for a task) build their items and questions and call the same method instead of a second executor.

## Native routes and Settings

Extensions use the same executor through owner-bound `host.decide` (`extensions/host.md` -> Native Decisions). The bundled Shell Review policy consumes native probabilities; its authorization rules and failure handling belong to `extensions/shell-review.md`.

`DecisionWire` is independent of the Chat protocol. It declares the native protocol, an absolute endpoint path at the configured Provider origin, an optional Model-specific `state_field`, and explicitly allowed task controls (`../providers/wire-profiles.md`). A compatible Provider can declare the same implemented protocol; adding a Provider never requires a second DecisionService or a Chat-completion simulation.

| Bundled Provider / Connection | Protocol and endpoint | Settings controls |
|---|---|---|
| OpenRouter / `api-key` | `system_one`, `/api/alpha/decisions` | `provider_options`, forwarded as native `provider` |
| OpenAI / `api-key` | `openai_decisions`, `/v1/decisions` | `prompt_cache_key`, at most 64 characters |
| Ollama / `local` | `system_one`, `/v1/systemone` | `keep_alive` |
| Ollama Cloud / `api-key` | `system_one`, `/v1/systemone` | None |

The OpenAI subscription Connection has no Decision route. Native Decisions do not offer Chat sampling, Reasoning or output-token settings; there is no `extra_options` generation escape hatch. Empty controls are omitted. OpenRouter discovery includes `GET /api/v1/models?output_modalities=decisions`; Ollama discovery projects its reported `decision` capability. See the Provider maps for catalog ownership.

Every wire encodes structured state as compact JSON text before transport; JSON keys remain evidence and cannot activate a Provider's nested image/media interpretation. `system_one` sends that text as `state` plus a map of questions; the exact `scaledown/classify-1` rule wraps it as `state.text`. When the protocol cannot represent a caller's question id, the client substitutes safe ids for the whole request and restores the caller's ids on receipt. `openai_decisions` uses `input` plus a question array: predicates, value/description choices and labeled score levels; returned arrays normalize to the same canonical answer map. These are native protocols, not instructions asking a Chat Model to produce JSON.

The three exact OpenRouter ids `respan/span-01`, `respan/span-01-lite` and `respan/span-01-lite:free` have `decision.protocol: none`: raw 2026-10-30 requests reject general choice/score questions and arbitrary JSON state. This restriction does not apply to `respan/span-02` or future Model ids. A Model name or missing per-version probe is otherwise no reason to exclude a cataloged Decision Model.

## Judgments

State is text, a JSON object or a JSON array. Questions have unique ids, complete instructions and type-specific criteria. The canonical vocabulary is `noul` (probability of yes, 0 to 1; optional `true`/`false` criteria), `choice` (2 to 255 label-to-description entries, labels at most 128 characters) and `score` (2 to 10 ordered level descriptions, answered as an expected zero-based level, possibly fractional). A midpoint score does not prove evidence for the middle level: uncertainty between distant levels can have that expectation. The `classify` Tool presents `yes_no`, `options` and `levels` and converts them before calling. Images and generated explanations are unsupported even if a selected native Model has other media capabilities.

Responses validate matching ids, types, labels, finite ranges and any supplied distributions or Usage fields. A native per-question refusal becomes `{"type": "refusal"}` without invalidating other answers; the Tool shows that question as declined. Missing probabilities, confidence and token counters stay absent. Confidence is Provider-reported, not a guarantee of accuracy or a value comparable across Models.

Missing evidence is not a negative judgment or an out-of-category case. When an unanswered question needs an explicit result, use a `choice` with an `unknown` option for insufficient information and a separate catch-all for other categories. Use code for exact counts, calculations, date comparisons and exact missing-field tests. This guidance does not assert that every Decision Model has Jev's limitations; it keeps deterministic checks out of probabilistic judgments. Native contracts: [OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions), [Ollama System One](https://docs.ollama.com/api/systemone), [TypeSafe primitives](https://docs.typesafe.ai/primitives).

## Selection facts and limits

Targets expose supported question types, input, question, choice and level limits, and the Decision input price per million tokens when known. Settings displays these facts alongside the selected target; they are not mutable generation controls or recommendations.

`capabilities.task_options.decision.limits` owns published task-specific bounds. A Decision-only Model can fall back to its catalog Context window (Connection-specific when reported). A Model that also serves Chat cannot transfer its Chat Context or pricing to its separate Decision endpoint; absent Decision facts remain unknown. Do not derive restrictions from community descriptions, a family's name or an upstream endpoint with a different renderer. In particular, the old blanket 2,000-token Clef cutoff is not a Provider contract and is not enforced.

`decision_pricing.py::pricing_for_decision` first reads `task_options.decision.pricing` in the ordinary `TokenPricing` format; a decisions-only Model can fall back to its exact catalog price. Mixed Chat/Decision Models never borrow the Chat price. `DecisionProfile` freezes this task price with the request's other facts. `price_decision_usage` values both raw response observations and validated results, preserving charges even if answer validation fails: finite Provider-reported cost wins, otherwise measured counters and known task rates yield an estimate; absent rates produce an explicit unavailable cost instead of the ledger's Chat-price fallback. No estimate becomes `reported_cost_usd` (`../usage.md`).

## Execution

- Invalid questions or items, more than `ITEM_LIMIT` (100) items, empty `context`, a missing binding and questions exceeding published Model question/choice/level limits raise `DecisionError` before any request is sent. Item types are checked before wrapping them with `context`. Execution retains one validated target, profile and deep-copied options snapshot; concurrent Settings/catalog changes cannot substitute another target or contract for later items.
- `decision_payload` builds the same complete body for preflight and transport, including Model, all questions, protocol encoding and selected controls. Copying and sizing run on bounded `decision-setup` workers. An oversized item alone yields `too_large` without a request: the best-effort input estimate must fit the reported task window, and any published request/text-byte bound must fit. The text-byte guard conservatively counts the serialized text-only request, not just the raw state. Without a reported bound no such guard is invented. vBot's 1 MB finite-JSON input limit also applies.
- Decision Usage can count shared evidence once per question, so billed `input_tokens` are not the Context footprint. This executor neither learns input-estimate calibration samples nor applies a stored calibration factor. Provider size validation remains authoritative: HTTP 413, or HTTP 400 with the explicit Context-overflow markers in `decisions.py`, becomes `too_large`; unrelated 400s stay `provider_error`.
- Each item uses the existing Connection credential and shared `ProviderTaskClient` HTTP/auth/retry owner. The replay-safe judgment policy retries transport failures, 429, 502-504 and the documented Decision edge/overload statuses 524/529 with shared backoff; OpenRouter's in-flight budget 402 is also retryable (`../providers/request-policy.md`). Ordinary 500/4xx responses are not repeated. A non-JSON or malformed native answer is `invalid_result`.
- At most 8 items run at once. Each item owns its request client and borrows the Runtime's Provider/Connection/Account HTTP pool, which remains reusable across calls; see `../providers.md` for transport ownership.
- A failed item yields its `DecisionError` in its place; the other items still complete. Codes: `invalid_request`, `not_configured`, `too_large`, `provider_error` (also once the retries are spent), `invalid_result`.
- Every Provider attempt writes canonical `decision` Usage with the caller's `TaskUsageContext` (Run/Session/Project and Extension scope); optional Provider counters and cost remain unknown when absent.

## Retired database

The former `decisions` database (`<data-dir>/decisions.db`) is listed in `core/runtime/databases.RETIRED_CANONICAL_DATABASES`. A normal Runtime start quarantines and unregisters it through `retire_core_databases` (`../database/snapshots-and-recovery.md` -> Unregister and retire); an update's verification start does not, because its rollback needs every registration unchanged. Keep the entry while installations may still carry the file.

## Verification owners

- `tests/core/model_tasks/test_decisions.py`: bounded per-item requests, partial failure, token/byte/question limits, Provider size errors, immutable binding/profile/options snapshots, and mocked native OpenAI/Ollama service paths.
- `tests/core/model_tasks/test_model_task_targets.py`: catalog tags plus native Connection routes, compatible new Providers, credential gates, selection facts and visible-label stability.
- `tests/core/model_tasks/test_decision_{types,providers}.py`: canonical validation, optional telemetry, native question/answer translation, refusals, exact requests/auth and replay-safe retries. `test_options.py` owns native Decision controls; `tests/core/providers/test_wire_profiles.py` owns route declarations and layering.
- `tests/core/model_tasks/test_task_usage.py`: `decision` Usage per request.
- `tests/core/runtime/test_runtime_lifecycle.py::test_start_modes_split_staging_cleanup_conversion_and_database_release`.

## Live verification (2026-10-30)

Through the actual TaskModelService, DecisionService and ProviderTaskClient with development credentials, 12 synthetic calls succeeded across six targets: OpenRouter `~typesafe/jev-latest`, `cloudflare/clef`, `liquid/d1`, `openai/gpt-6-luna-decisions`, `scaledown/classify-1`, and Ollama Cloud `clef:27b`. Each target answered all three primitives; a second call distinguished insufficient evidence (`UNKNOWN`) from explicit evidence outside the categories (`OTHER`). Caller ids containing Unicode and punctuation survived native id mapping, and image-shaped JSON remained text evidence. These small fixtures verify transport and integration, not general judgment quality. OpenAI Platform was implemented against its official Decisions reference and mocked tests, without a direct inference call.

A separate raw OpenRouter probe pinned `cloudflare/clef` to the Cloudflare upstream without fallbacks. A marker after about 6,000 tokens yielded probability 0.9932 when present (6,169 reported input tokens) and 0.0101 when absent (6,168). This contradicts applying the older Community guide's roughly 2,000-token truncation statement as a universal limit; it does not establish maximum capacity. Selection and guards use reported catalog limits, not this probe's input length. Account-level ignored-provider 404s and a Cloudflare Flash 429 are access/rate outcomes, not evidence for a Model compatibility exclusion.
