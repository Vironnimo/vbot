# Google AI Studio Provider

This supplementary map covers the `google` Provider: Google's own Gemini API at `generativelanguage.googleapis.com`, reached with an AI Studio API key.

## Boundary

`core/providers/google.py::GoogleAdapter` subclasses the shared native Gemini codec `GeminiCompatibleAdapter` (`gemini_compatible.py`, `_gemini_wire.py`; its request, replay, stream and usage contracts are documented in `providers/opencode-zen.md` -> Reasoning, Replay, and Prompt Caching and -> Limits, Sampling, Tools, and Media, since OpenCode Zen and Go compose the same codec). The Adapter adds only what is specific to Google's endpoint: catalog discovery and normalization, and Google's error semantics. Request rules (protocol `gemini`, reasoning ladder fallback, media types and the documented 100 MB request payload limit) are wire-profile data in `resources/wire/google.json`; Provider and Connection live in `resources/providers/google.json`.

## Connection and discovery

- One `api-key` Connection: `x-goog-api-key` header, credential `GEMINI_API_KEY`, base `https://generativelanguage.googleapis.com/v1beta`. Chat uses `POST /models/{id}:streamGenerateContent?alt=sse` (fallback `:generateContent`).
- Discovery reads `GET /models?pageSize=1000` (Google's maximum page size; the default page of 50 would truncate the lineup). Entries are `{"name": "models/<id>", ...}`; the Adapter strips the `models/` prefix, keeps `inputTokenLimit`/`outputTokenLimit` as Context window and output ceiling, and maps the boolean `thinking` to reasoning support. The listing reports no modalities, so inputs stay text until models.dev enrichment (`models_dev_id: google`) widens them and supplies the reasoning ladder through the canonical `google/<id>` entry.
- Discovery classifies native Models by their listed methods and id families. `embedContent` and `createDecision` identify embeddings and Decisions, and the `generateContent` families identify Chat, speech, image, music and video. `bidiGenerateContent` entries are kept for the Live voice family. Catalog classification and executable Task Model routes are separate (`model_tasks.md`). Interactions agents (`antigravity-*` and `deep-research-*`) are skipped; Antigravity rejected `generateContent` in a live probe on 2026-11-17.

## Errors and quota

- An invalid key is HTTP 400 `INVALID_ARGUMENT` with `ErrorInfo.reason: API_KEY_INVALID`; the Adapter raises `ProviderAuthError` for it. Other 400s keep the shared classification.
- Google reports every quota failure as HTTP 429 `RESOURCE_EXHAUSTED`. A zero limit (`limit: 0` in the message: the free tier does not include the Model) or a daily quota (`QuotaFailure` `quotaId` containing `PerDay`) is a non-retryable `ProviderError`, so Chat does not burn its recovery budget on hours-long waits. Other 429s stay retryable `ProviderRateLimitError`. Without a `Retry-After` header, `RetryInfo.retryDelay` or a "retry in Ns" message supplies the floor, including fractional seconds. Shapes captured live on 2026-11-17 with a free-tier key.
- 503 `UNAVAILABLE` ("high demand") is common on new Models and stays retryable under the shared policy, also when reported in-band after the stream opened.

## Verification

Free-tier key, 2026-11-17; latency 30-230 s per request under Google's load at the time.

- Catalog: 27 chat Models after filtering; `gemini-4-hydrogen` joins its canonical `google/gemini-4-hydrogen` entry (effort ladder `low/medium/high/xhigh`).
- `gemini-4-hydrogen` through `scripts/verify_wire_profile.py`: send, stream, Tool replay (signed parts, effort `xhigh`) and each effort `low`, `medium`, `high`, `xhigh` passed. The key then hit the Model's daily free-tier quota, which the Adapter classified as non-retryable; `none`, sampling, image and the replay measurement were not run on it. No `verified` record is written, so its wire status stays `inferred`.
- The replay measurement passed on `gemini-3-flash-preview` (`full_history` consumed in-Run and across Runs) once unsigned Function Calls carried the placeholder signature (`providers/opencode-zen.md` -> Reasoning, Replay, and Prompt Caching).
- Many other Models returned 429 `limit: 0` on that key; that is an account entitlement, not a Model fact, and none of them is excluded for it.
