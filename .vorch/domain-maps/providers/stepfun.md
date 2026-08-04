# StepFun Provider

Read `providers.md` first. This reference owns vBot's explicit StepFun Direct API and Step Plan Connections plus the Provider-specific policy layered onto the shared OpenAI Chat Completions transport.

## Connections and auth

- `stepfun:direct-api` uses `STEPFUN_DIRECT_API_KEY` against the usage-based `https://api.stepfun.com/v1` endpoint. `stepfun:step-plan` separately uses `STEPFUN_API_KEY` against the subscription-only `https://api.stepfun.com/step_plan/v1` endpoint.
- Both Connections use `Authorization: Bearer` API keys, but their credential slots, endpoints, Model allowlists, and billing/entitlement behavior remain explicit and independent. Credential contents never select or rewrite a Connection.
- The obsolete Hermes host `api.stepfun.ai` is not an endpoint alias. No additional regional Connection is bundled because the current official contract fixes both surfaces to `api.stepfun.com`.

## Request and response policy

- `StepFunAdapter` extends `OpenAICompatibleAdapter`; the shared Adapter owns Chat messages, Tool schemas/calls/results, response and SSE normalization, usage, retry classification, and canonical terminal outcomes. A narrow shared stream-payload hook lets StepFun enable SSE without sending the undocumented `stream_options` extension.
- The Adapter normalizes `max_tokens`, `max_completion_tokens`, and `max_output_tokens` into the single documented `max_tokens` field. It validates `temperature` in `[0, 2]`, `top_p` in `(0, 1]`, `frequency_penalty` in `[-2, 2]`, and `n == 1`, and rejects OpenAI extensions StepFun does not document instead of relying on permissive server behavior.
- `step-3.7-flash` and `step-router-v1` accept `low`/`medium`/`high`; `step-3.5-flash-2603` accepts `low`/`high`; base `step-3.5-flash` has reasoning but no documented effort control, so selected effort is omitted. Reasoning replay stays `current_run`; StepFun documents replaying ordered Assistant content and Tool calls/results, not full historical reasoning.
- StepFun returns `reasoning` and the equivalent `reasoning_content`; the shared normalizer accepts both. Prompt caching is automatic for the three Step Flash Models after 256 prompt tokens and reports `prompt_tokens_details.cached_tokens`; vBot adds no cache-control field.
- The native vBot wire carries StepFun's documented JPEG, PNG, static GIF, and WebP inputs. `step-3.7-flash` also supports video remotely, but vBot has no OpenAI-compatible `video_url` encoder and therefore degrades video rather than claiming native delivery. The selected text Models do not expose StepFun's separate audio, image-generation, or realtime APIs.
- HTTP 402 is a non-retryable balance/Plan-entitlement failure and HTTP 451 is a non-retryable safety-review rejection. Auth failures, rate limits, transient 5xx failures, Tool terminal outcomes, output truncation, incomplete streams, and malformed responses retain the shared Provider policy.

## Catalog policy

- Both Connections discover through their own authenticated `/models` endpoint. Discovery persists the full raw response, exposes only the current agentic Chat allowlist, and stamps each normalized Model with the Connection that returned it.
- The bundled fallback contains exact wire ids only: `step-3.5-flash`, `step-3.5-flash-2603`, and `step-3.7-flash` on both Connections, plus `step-router-v1` on Step Plan only. Audio, realtime, image-generation, legacy, unknown, and retired ids stay in raw discovery but are not projected into vBot's Chat Model picker.
- `step-router-v1` is a visible Plan-only routing Model, not an alias or fallback. StepFun currently routes it between `deepseek-v4-pro` and `step-3.7-flash`; vBot never rewrites the requested id or silently substitutes another Model. Its documented `max_tokens` ceiling is 250,000 and image/document input is not offered.
- Connection-scoped catalog replacement removes only the refreshed Connection membership from an existing entry, preserves memberships from other Connections, and unions them back when the refreshed Model is shared. Refreshing one StepFun surface therefore cannot erase the other surface's allowlist.

## Verification

- Request, sampling, reasoning, media, response, cache-usage, SSE, and error policy: `tests/core/providers/test_stepfun.py`
- Connection-scoped discovery, raw audit, exact allowlist, and shared-membership merge: `tests/core/models/test_discovery_provider_refresh.py`
- Bundled Connection config, fallback Catalog, and Runtime Adapter selection: `tests/core/runtime/test_runtime_providers.py`
