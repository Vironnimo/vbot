# Kimi Provider

This supplementary map covers Kimi Coding Plan and Moonshot Platform Connections, current Model policy, reasoning replay, and multimodal Chat Completions behavior.

## Boundary

`core/providers/kimi.py` owns Kimi-specific Connection mode, Model facts, request shaping, reasoning replay, output-limit aliases, cache affinity, and multimodal limits while reusing the deep OpenAI-compatible transport. Provider and Connection declarations live in `resources/providers/kimi.json`; durable Model facts, exact wire ids, and Connection allowlists live in `resources/models/kimi.overrides.json`. Generic credential resolution, discovery, HTTP retries, Tool rendering, and Chat response normalization stay in their shared owners.

## Connections and discovery

- `coding-plan` uses `KIMI_CODING_API_KEY` and the OpenAI-compatible `https://api.kimi.com/coding/v1` surface. `api-key` uses `KIMI_API_KEY` at `https://api.moonshot.ai/v1`; `api-key-cn` uses `KIMI_CN_API_KEY` at `https://api.moonshot.cn/v1`. All three discover Models through `/models`.
- Connections are explicit. Never infer the Kimi product or host from a credential prefix: the same Provider intentionally separates Subscription, global Platform, and China Platform credentials and Model allowlists.
- Coding Plan exposes only `k3`, `k3-256k`, `kimi-for-coding`, and `kimi-for-coding-highspeed`. Platform Connections expose the current direct ids `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.7-code-highspeed`, and `kimi-k2.6`. Retired K2 preview/thinking aliases are discovery exclusions; K2.5 is not statically advertised because it is restricted for new Accounts and scheduled for retirement, though discovery may retain it for an entitled existing Account.
- Keep the exact `kimi-for-coding-highspeed` spelling. Kimi may silently fall back to the standard coding Model for an invalid HighSpeed id, so aliases must never be guessed or normalized on the wire.
- A Coding Plan 401 may mean insufficient plan entitlement rather than an invalid key. It remains a terminal auth/entitlement result and must not be retried or silently routed to another Connection.

## Request and reasoning policy

- All Connections use OpenAI-compatible `/chat/completions` and `max_completion_tokens`; deprecated `max_tokens` and vBot's output aliases are collapsed to the smallest valid requested limit. K3 uses a 131072-token default ceiling; K2.6/K2.7 use 32768 because Kimi rate limiting accounts for the requested output allowance.
- Kimi's current Models have Model-specific sampling constraints, so `temperature`, `top_p`, and `n` are omitted rather than forwarding generic Provider defaults.
- K3 uses `reasoning_effort: low|high|max`: vBot `minimal|low` maps to `low`, `medium|high` to `high`, and `xhigh|max` to `max`. Platform K3 always reasons, so `none` degrades to `low`; Coding Plan `none` sends `thinking.type: disabled`, which Kimi documents as routing the request to K2.6.
- K2.6 uses `thinking.type: enabled|disabled`; enabled requests set `thinking.keep: all` so persisted `reasoning_content` remains valid across turns. Platform K2.7 Code is fixed to enabled/all. Coding Plan's K2.7 aliases honor `none` by disabling thinking, with the same documented K2.6 routing consequence.
- Reasoning-capable Models replay canonical Assistant reasoning as `reasoning_content` across same-Model history. A non-reasoning Model strips replay; an unprofiled discovered Model keeps only current-Run reasoning until its behavior is known.
- `prompt_cache_key` is derived from the cache-affinity id, falling back to stable Agent/Session identity. Keep it on Coding Plan requests: Kimi requires it for effective Subscription caching and recommends it for coding agents generally.

## Multimodal policy

- Kimi accepts vBot's recognized JPEG, PNG, GIF, and WebP images and MP4, MOV, and WebM videos when the selected Model advertises that input modality. `k3-256k` is image-only; the other statically profiled Kimi Models accept image and video.
- Images encode as base64 `image_url` parts and videos as base64 `video_url` parts. Native video resolution is provider-scoped: other OpenAI-compatible Adapters continue rejecting video unless they explicitly override the user-content encoder and advertise a matching wire type.
- Only current-turn media bytes are sent natively; historical video remains a stored-path note. The request body is rejected locally above 100,000,000 bytes on Platform Connections and above the conservative 80 MiB Coding Plan limit. vBot's attachment store may impose a lower per-file limit first.

## Verification

`tests/core/providers/test_kimi.py` covers reasoning ladders and switches, replay, output aliases, cache affinity, sampling removal, media encoding, request limits, and catalog normalization. `test_block_resolver.py` covers Model-plus-wire video gating; Runtime/config/catalog wiring and discovery registration live in `test_runtime_providers.py` and `test_discovery_models_dev.py`. Live credentials are unavailable, so actual entitlement responses, catalog drift, cache accounting, large-media behavior, and inference/stream payloads remain live-verification items.
