# MiniMax Provider

MiniMax owns three Connections behind one Adapter: global and China API/Token Plan keys use OpenAI-compatible Chat Completions, while browser login uses MiniMax's Anthropic-compatible Messages endpoint.

## Interfaces

- Provider config: `resources/providers/minimax.json`
- Adapter selector: `minimax`
- Adapter class: `MiniMaxAdapter`
- Global key Connection: `minimax:api-key`, `https://api.minimax.io/v1`, `MINIMAX_API_KEY`
- China key Connection: `minimax:api-key-cn`, `https://api.minimaxi.com/v1`, `MINIMAX_CN_API_KEY`
- Subscription Connection: `minimax:subscription`, `https://api.minimax.io/anthropic/v1`, MiniMax OAuth tokens
- Key Connections call `POST /chat/completions` and discover through `GET /models`; the subscription Connection calls `POST /messages` and discovers through `GET /models` on its Messages base.
- Durable shipped model facts and Connection restrictions live in `resources/models/minimax.overrides.json`; a credentialed refresh may additionally publish `resources/models/minimax.json`.

## Wire Contract

- Global/China Chat requests use MiniMax's OpenAI-compatible `/v1/chat/completions` API with `Authorization: Bearer <key>`. Pay-as-you-go and Token Plan keys use the matching key Connection.
- Browser-login requests use `/anthropic/v1/messages` with `Authorization: Bearer <OAuth access token>`. An inner `AnthropicCompatibleAdapter` that shares the outer Adapter's HTTP client and wire-profile binding reuses Messages Tool, streaming, signed-thinking replay, Usage, and prompt-cache mechanics. `resources/wire/minimax.json` selects the `messages` protocol for the `subscription` Connection and there drops Claude-specific thinking/effort controls (dialect `none`), sets prompt-cache breakpoints, and declares no media.
- MiniMax accepts `temperature` only in `(0, 1]`; both wires reject non-finite, zero, negative, or above-one values locally before network I/O (wire profile parameter rule with `out_of_range: reject`).

## Models And Discovery

- MiniMax's model listings are sparse and require credentials, so `minimax.overrides.json` makes the verified text roster selectable before the first refresh; `MiniMaxAdapter.normalize_catalog_entry()` fills the same known facts when discovery runs.
- Known M2.x models normalize as text-input chat models with 204,800 token context windows, tools, and `reasoning_split`.
- `MiniMax-M3` normalizes with a 1,000,000 token context window, text/image/video input metadata, tools, `stream_options`, `reasoning_split`, and MiniMax `thinking`.
- Unknown MiniMax model ids fall back to generic OpenAI-compatible normalization instead of being dropped.
- The subscription Connection exposes only `MiniMax-M2.7` and `MiniMax-M2.7-highspeed`: Hermes' current subscription roster and MiniMax's Token Plan docs support those, while MiniMax's Anthropic-compatible documentation does not yet list M3. M3 and older M2.x releases remain direct-key only until that wire is verified.
- Known MiniMax entries carry an output ceiling (`max_output_tokens`) set to MiniMax's **recommended** allowance, not its hard max: M2.x = 65,536, M3 = 131,072 (`MINIMAX_M2_RECOMMENDED_MAX_OUTPUT` / `MINIMAX_M3_RECOMMENDED_MAX_OUTPUT`). MiniMax publishes both a recommended and a hard-max value per model (M2.x hard max 204,800 - which equals the context window; M3 hard max 524,288); vBot pins the recommended value because the M2.x hard max equals the context window, so defaulting the output allowance to it would collide with any non-trivial prompt and 400. A caller can still request more explicitly (up to the hard max). Source: https://platform.minimax.io/docs/api-reference/text-chat-openai.
- Runtime effect: the shared OpenAI-compatible base defaults request `max_tokens` to this ceiling when the caller sends no explicit output limit (see `providers/request-policy.md` -> Request limits and context), so a MiniMax turn is no longer capped at the flat 8,192 `minimax.json` config default - reasoning models truncated there because the split thinking trace counts toward the same allowance.

## Reasoning

- MiniMax's OpenAI-compatible API does not use OpenAI-style `reasoning_effort`. The Chat Completions protocol of `resources/wire/minimax.json` drops generic reasoning keys (`reasoning`, `reasoning_effort`, `include_reasoning`, `thinking`) and renders MiniMax controls through its dialects.
- For `MiniMax-M3` on the key Connections, `ReasoningWire.plan()` (see `providers/request-policy.md` -> Reasoning intent) classifies the selection and the `minimax_thinking` dialect maps it onto MiniMax's binary toggle: an active effort (incl. `budget`/`on` - M3 has no native token budget) -> `thinking: {type: adaptive}`, `none`/off -> `thinking: {type: disabled}`, no effort selected -> reason-by-default (no `thinking` key).
- M2.x and unknown Models use the `minimax_split` dialect: `thinking` is dropped (those models reason by default).
- The adapter defaults `reasoning_split: true` whenever reasoning is active (M2.x always; M3 unless thinking is disabled), so the thinking trace is returned separately as `reasoning_details` instead of inline reasoning-delimiter markup in `content`. A caller-set `reasoning_split` is left alone; catalog reasoning-unsupported strips it. This is the capture half of the replay policy below - `reasoning_details` is what gets persisted in `reasoning_meta` and replayed.
- Non-streaming responses with `reasoning_details` expose their text as visible `reasoning` while preserving the original details in `reasoning_meta`.
- Reasoning replay inherits the shared `full_history` default for known and unknown Models unless an explicit top-level override narrows it, with the inherited `meta_preferred` fidelity: on key Connections the OpenAI wire replays captured `reasoning_details` without duplicating the visible plaintext; on the subscription Connection the Messages wire round-trips complete signed `thinking` blocks. The Messages protocol entry of `resources/wire/minimax.json` keeps the Chat Completions declaration (`meta_preferred`, history field `reasoning_content`) only because the outer Adapter budgets the subscription's output allowance and Context against the Chat Completions rendering of the history; the Messages serializer ignores fidelity and sends only signed blocks. Declaring `meta_only` there (or estimating with the Messages rendering) changes `max_tokens` on context-clamped requests - a wire change that needs live evidence. **Neither path has been probed against the live MiniMax API in this environment**; wire behavior is pinned by unit tests and live verification remains deferred in `.vorch/FLAGGED.md`.

## Subscription OAuth

- MiniMax's browser login is a provider-specific Device Flow selected by `oauth.device_flow: "minimax_oauth"`: authorization posts PKCE S256 plus state to `/oauth/code`, validates the echoed state, then polls `/oauth/token` with the `user_code` grant and verifier.
- MiniMax's `expired_in` is ambiguous in practice: it may be TTL seconds or an absolute Unix-millisecond timestamp. `resolve_minimax_oauth_expiry()` handles both for initial exchange and refresh.
- Access tokens are refreshed through the standard `refresh_token` grant on every request when expiring; the refresh POST is never replayed automatically. A terminal refresh failure deletes the dead MiniMax token so subsequent requests require reconnect instead of replaying a rotated/revoked refresh token; transient Provider failures retain it.
- PKCE verifiers stay in `DeviceFlowEngine` memory, keyed by Provider/Connection/Account/user code, and are removed on completion, cancellation, supersession, or engine close.

## Usage Probe (`token_plan/remains`)

The MiniMax usage fetcher in `core/providers/usage.py` (see `providers/usage.md`). **Blind, best-effort** - implemented from openclaw's verified field names, not yet live-verified (no credentials in this environment):

- `GET <connection.base_url>/token_plan/remains` on `minimax:api-key`, with `Authorization: Bearer <api key>`.
- Expected body: `model_remains[]`. The fetcher picks the non-zero-total chat entry whose `model_name` starts `minimax-m` case-insensitively, derives `used_percent = (total - remaining) / total`, then projects the interval label and reset time from known candidate fields.
- A body that is not a usable `model_remains` list, or has no qualifying chat model, produces snapshot error "Unsupported response shape", never a crash.
- **Caveat:** MiniMax misnames "usage" vs "remaining"; the remaining-count key is an
  assumption pinned only by unit tests until live-verified - flagged in `.vorch/FLAGGED.md`.

## Constraints & Gotchas

- The wire profile selects the wire: `resources/wire/minimax.json` sets the `messages` protocol for the `subscription` Connection, and `MiniMaxAdapter` routes a request to its inner Messages Adapter when the resolved profile's protocol is `messages`. Do not route the subscription through OpenAI Chat or direct keys through the subscription's bearer-Messages profile without live evidence.
- The output allowance rides on the wire as `max_tokens` for every MiniMax model. M3's OpenAI-compatible endpoint documents `max_completion_tokens` as the current key and marks `max_tokens` deprecated, but still accepts `max_tokens`; vBot sends `max_tokens` uniformly (the shared base builder) rather than branching the field name per model.
- `MiniMax-M3` documents image and video input only on the direct OpenAI path. The Messages compatibility endpoint is declared text-only (`media.types: []` on the Messages protocol) and degrades media before the wire.
- Durable MiniMax catalog facts belong in `MiniMaxAdapter.normalize_catalog_entry()`/`MINIMAX_MODEL_FACTS` and `minimax.overrides.json`, never in hand edits to generated `resources/models/minimax.json`.
