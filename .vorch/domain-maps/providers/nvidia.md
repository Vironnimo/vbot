# NVIDIA Provider

Read `providers.md` first. This reference owns vBot's NVIDIA API Catalog Connection (build.nvidia.com, free trial keys) and the policy layered onto the shared OpenAI Chat Completions transport.

Evidence status: the integration was built without an API key. The public Model listing was read live; inference behavior (auth, reasoning controls, media, errors, limits) comes from NVIDIA's per-Model API reference (`docs.api.nvidia.com/nim/reference/<publisher>-<model>`, read 2026-11-17) and is not live-verified. Verify with a key before treating any of it as proven (Verification below).

## Connections and auth

- `nvidia:api-key` sends `NVIDIA_API_KEY` (keys from build.nvidia.com start with `nvapi-`) as `Authorization: Bearer` to `https://integrate.api.nvidia.com/v1`. One key covers every hosted Model; there is no separate subscription Connection.
- Trial access is rate-limited rather than metered: NVIDIA retired the old credit balance and publishes no per-Model limit. A 429 follows the shared rate-limit policy, and a 402 (once used for exhausted credits) stays the shared non-retryable payment error; `NvidiaAdapter` adds no error policy of its own.

## Request and response policy

- `NvidiaAdapter` extends `OpenAICompatibleAdapter` only for the catalog filter; the shared Adapter owns messages, Tools, SSE, usage, retries and reasoning extraction (`reasoning_content`, a protocol default).
- Each hosted Model keeps its own reasoning control, so `resources/wire/nvidia.json` sets it per family: Nemotron 3 and 3.5 (`nvidia/nemotron-3` prefix) use the `chat_template_kwargs` dialect with `enable_thinking` true/false; Nemotron 3 Ultra adds `medium_effort: true` for its medium rung (ladder `medium`/`high`, `none` switches thinking off); gpt-oss takes `reasoning_effort` low/medium/high and spells off as `low`. Every other Model keeps the protocol default `reasoning_effort` with its catalog ladder.
- The `chat_template_kwargs` dialect is generic (`core/providers/reasoning_dialects.py`): `reasoning.options.switch` names the template switch and `reasoning.options.level_kwargs` adds template keys per rung, so Custom Providers on vLLM or SGLang can use it too. A caller or `request.extra_body` value for `chat_template_kwargs` replaces the rendered object; a `request.body_defaults` entry applies only when nothing was rendered.
- Images go as `image_url` data URIs; the wire defaults declare JPEG, PNG, GIF and WebP as documented for the vision Models.
- vBot streams every request. NVIDIA's cloud functions can answer a long request with 202 and an `NVCF-REQID` to poll; vBot has no polling path, and whether streamed integrate requests ever take it is unverified.
- Kimi K3.1 Pro has the hosted API's per-request output cap. Other output limits come from the catalog, the Provider default `max_tokens: 16384`, and rejection learning, which reads the vLLM "maximum context length is N tokens" wording.

## Catalog policy

- The 2026-11-19 listing added `moonshotai/kimi-k3.1-pro` and dropped `openai/gpt-oss-20b`. NVIDIA's [hosted Chat API reference](https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k3_1-pro-infer) specifies image URLs, Tool messages, preserved `reasoning_content`, a 65,536-token output ceiling and temperature from 0 to 1. Reasoning uses the catalog ladder with the lowest rung for off. Authenticated inference was unavailable when these facts were added.
- `/v1/models` is public (`catalog_requires_credentials: false`), so a refresh works without a key. The listing carries ids only, no task field: `NvidiaAdapter.accepts_discovered_model` drops embedding/reranking retrievers, guard and safety classifiers, `nvclip` and detectors by id part; every other entry is a chat Model.
- The bundled `resources/models/nvidia.json` (19 chat Models, 2026-11-17) is enriched from the models.dev `nvidia` section for context, output limits, modalities, reasoning ladders and pricing. That pricing is models.dev's list price, not the trial's free terms. Entries models.dev lacks (for example `bigcode/starcoder2-15b`, `nvidia/nemotron-3.5-bolt-e4b`) have unknown limits.
- NVIDIA's API reference also documents Models absent from the public listing (DeepSeek V4, Kimi K3, Nemotron 3 Super, gpt-oss-120b); vBot carries no profile entries for them until the listing offers them.

## Verification

- Catalog filter and per-Model reasoning on the wire: `tests/core/providers/test_nvidia.py`; descriptions: `tests/core/providers/test_reasoning_describe.py`.
- With a key: `python scripts/verify_wire_profile.py --provider nvidia --model <id> --data-dir <data dir>` for at least one Nemotron (`chat_template_kwargs`) and one `reasoning_effort` Model (`providers/compatibility-verification.md`).
