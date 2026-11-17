# Google AI Studio Provider

This supplementary map covers the `google` Provider: Google's own Gemini API at `generativelanguage.googleapis.com`, reached with an AI Studio API key.

## Boundary

`core/providers/google.py::GoogleAdapter` subclasses the shared native Gemini codec `GeminiCompatibleAdapter` (`gemini_compatible.py`, `_gemini_wire.py`; its request, replay, stream and usage contracts are documented in `providers/opencode-zen.md` -> Reasoning, Replay, and Prompt Caching and -> Limits, Sampling, Tools, and Media, since OpenCode Zen and Go compose the same codec). The Adapter adds only what is specific to Google's endpoint: catalog discovery and normalization, and Google's error semantics for Chat and every Task Model client. Request rules (protocol `gemini`, reasoning ladder fallback, media types and the documented 100 MB request payload limit) are wire-profile data in `resources/wire/google.json`; Provider and Connection live in `resources/providers/google.json`.

## Connection and discovery

- One `api-key` Connection: `x-goog-api-key` header, credential `GEMINI_API_KEY`, base `https://generativelanguage.googleapis.com/v1beta`. Chat uses `POST /models/{id}:streamGenerateContent?alt=sse` (fallback `:generateContent`).
- Discovery reads `GET /models?pageSize=1000` (Google's maximum page size; the default page of 50 would truncate the lineup). Entries are `{"name": "models/<id>", ...}`; the Adapter strips the `models/` prefix, keeps `inputTokenLimit`/`outputTokenLimit` as Context window and output ceiling, and maps the boolean `thinking` to reasoning support. The listing reports no modalities: task families get theirs from the Adapter, while Chat inputs stay text until models.dev enrichment (`models_dev_id: google`) widens them and supplies the reasoning ladder through the canonical `google/<id>` entry.
- Discovery classifies native Models by their listed methods and id families. `embedContent` and `createDecision` identify embeddings and Decisions, and the `generateContent` families identify Chat, speech, image, music and video. `bidiGenerateContent` entries are kept for the Live voice family. Each family's `task_options` (voices, aspect ratios, durations, thinking levels, limits) are hand-maintained facts in `core/providers/google.py` beside their documentation source, so a changed Google lineup needs a code change plus a catalog refresh. Catalog classification and executable Task Model routes are separate (`model_tasks.md`). Interactions agents (`antigravity-*` and `deep-research-*`) are skipped; Antigravity rejected `generateContent` in a live probe on 2026-11-17.

## Task Models

Every Task Model wire selects Google by Adapter `google`, reuses the Connection's key header and base URL, and classifies HTTP statuses through `GoogleAdapter.classify_task_http_status()` (Errors and quota below). Wire details live with each task:

- Decision: `POST /v1beta/decisions`, protocol `google_decisions` from the wire profile's `connections.api-key.decision` -> `model_tasks/decisions.md`.
- Embeddings: `:batchEmbedContents`, at most 100 texts per request -> `model_tasks/embeddings.md` -> Google native route.
- TTS and STT (Gemini TTS and Transcribe Models): `:generateContent` -> `model_tasks/speech.md` -> Google route.
- Image generation (Nano Banana family): `:generateContent` with `imageConfig` -> `model_tasks/image.md`; option rendering -> `model_tasks/media-profiles.md`.
- Music (Lyria): `:generateContent` -> `model_tasks/music.md` -> Google route.
- Video (Gemini Omni): Interactions API, background interaction polled to completion; Veo 3.1 Preview was shut down on the Gemini API on 2026-10-22 -> `model_tasks/video.md` -> Google route.
- Live voice (`*-live*`, `*-native-audio-*`): `BidiGenerateContent` WebSocket -> `model_tasks/live-wires.md` -> Google wire.

## Errors and quota

- An invalid key is HTTP 400 `INVALID_ARGUMENT` with `ErrorInfo.reason: API_KEY_INVALID`; the Adapter raises `ProviderAuthError` for it. Other 400s keep the shared classification.
- Google reports every quota failure as HTTP 429 `RESOURCE_EXHAUSTED`. A zero limit (`limit: 0` in the message: the free tier does not include the Model) or a daily quota (`QuotaFailure` `quotaId` containing `PerDay`) is a non-retryable `ProviderError`, so Chat does not burn its recovery budget on hours-long waits. Other 429s stay retryable `ProviderRateLimitError`. Without a `Retry-After` header, `RetryInfo.retryDelay` or a "retry in Ns" message supplies the floor, including fractional seconds. Shapes captured live on 2026-11-17 with a free-tier key.
- Other endpoints vary the shape, and `_classify_google_status` accepts each: the Interactions API returns an array-wrapped body (`[{"error": {...}}]`), a lowercase string `code` (`resource_exhausted` with `limit: 0 ...` in the message; `quota_exceeded` for a daily quota), and the Decisions API names the wait only in its message. The Live API reports setup failures as WebSocket closes, which its wire maps to an HTTP status before classifying; an invalid key then arrives only as the message `API key not valid`, which the classifier also matches.
- 503 `UNAVAILABLE` ("high demand") is common on new Models and stays retryable under the shared policy, also when reported in-band after the stream opened.

## Verification

Free-tier key, 2026-11-17; latency 30-230 s per request under Google's load at the time.

- Catalog: 53 Models after filtering, 23 of them Chat; `gemini-4-hydrogen` joins its canonical `google/gemini-4-hydrogen` entry (effort ladder `low/medium/high/xhigh`).
- `gemini-4-hydrogen` through `scripts/verify_wire_profile.py`: send, stream, Tool replay (signed parts, effort `xhigh`) and each effort `low`, `medium`, `high`, `xhigh` passed. The key then hit the Model's daily free-tier quota, which the Adapter classified as non-retryable; `none`, sampling, image and the replay measurement were not run on it. No `verified` record is written, so its wire status stays `inferred`.
- The replay measurement passed on `gemini-3-flash-preview` (`full_history` consumed in-Run and across Runs) once unsigned Function Calls carried the placeholder signature (`providers/opencode-zen.md` -> Reasoning, Replay, and Prompt Caching).
- Task Models live on the same key: Decision (`gemini-decision-1`), embeddings (`gemini-embedding-2`), TTS (3.8 Flash, 3.8 Flash Lite, 3.1 Preview), Transcribe and Live voice (3.8 Live, 3.1 Flash Live, 2.5 native audio) worked. Image generation, Lyria and Omni answered 429 `limit: 0` (paid only), so their wires rest on Google's documentation and mocked tests; each task map records its own checks.
- Many other Models returned 429 `limit: 0` on that key; that is an account entitlement, not a Model fact, and none of them is excluded for it.
