# OpenAI Codex Image Generation - subscription task wire

Deep reference for the `openai/gpt-image-2::subscription` image-generation wire (the ChatGPT Plus/Pro Codex responses endpoint). Read this only when building on or debugging subscription image generation; orientation lives in `openai.md` -> Codex Image Generation.

This is an **internal/undocumented wire**, live-verified against the real ChatGPT Plus/Pro subscription connection on 2026-07-03. If it breaks, re-verify the raw wire (playbook below) before changing model visibility or UI behavior.

Endpoint + headers: `POST https://chatgpt.com/backend-api/codex/responses` with the Codex header recipe - `Authorization: Bearer <fresh OAuth token>`, `chatgpt-account-id` derived from the current JWT via `extract_chatgpt_account_id`, plus `CODEX_EXTRA_HEADERS`.

## Request shape

Carrier model `gpt-6-luna` (`_OPENAI_CODEX_IMAGE_CARRIER_MODEL`), `stream: true` (mandatory), `store: false`, instructions that ask for exactly one `image_generation` call that keeps the requested subject, and, if the call fails, one sentence with the reported reason instead of a retry (`_OPENAI_CODEX_IMAGE_INSTRUCTIONS`), one user `input_text`, and `tools: [{"type": "image_generation", ...}]`. The carrier must be a Model listed by `/codex/models` (`resources/models/openai.json` entries with `connections: ["subscription"]`); an unlisted slug can be withdrawn without notice. History: `gpt-5.5` was the carrier until it left `/codex/models` on 2026-10-08. The same day both `gpt-6-luna` and `gpt-5.6-luna` produced an image and, for a refused prompt, made one failed tool call followed by a one-sentence reason; `gpt-6-luna` was chosen as the cheapest and fastest listed Model.

## Tool options

The accepted tool fields are `output_format`, `output_compression`, `moderation`, `background`, `size`, and `quality`; `output_format` and `output_compression` are honored, `background` is validated (`transparent` is rejected for the current backend model), and `size`/`quality` are accepted but advisory, so the image client also weaves requested size/quality/background into the prompt text. Do not send `n` or `model`: `n` is rejected as an unknown parameter and `model` is silently forced by the backend to the `gpt-image-2-codex` family.

## Response shape

The response is SSE. Ignore progressive `response.image_generation_call.partial_image` frames; the final Base64 image is in `response.output_item.done.item` where `item.type == "image_generation_call"` and `item.result` contains the image. The final item also carries actual `output_format`, `quality`, `size`, `background`, and `revised_prompt`. Usage comes from `response.completed.response.tool_usage.image_gen` plus carrier `response.completed.response.usage`. The image task client buffers the stream and uses a 300-second timeout for this path.

## Re-verification playbook

1. **Product check:** configure `image_generation` to `openai/gpt-image-2::subscription`, generate one image through the image Tool, and expect the returned file under the caller-owned `image-gen/` directory (`<Workspace>/image-gen/` for an Identity Agent, `<Project cwd>/image-gen/` for a Project Config Agent). When the Assistant references that path, confirm the Accessor can render it through the ordinary signed `/api/files/` delivery path.
2. **Raw-HTTP check if the product path fails:** load the token from the OpenAI subscription OAuth token file, derive `chatgpt-account-id` from the JWT, post the minimal payload above to `/codex/responses` with `stream: true`, `store: false`, the carrier, one `input_text`, and `tools: [{"type": "image_generation"}]`, then confirm a `response.output_item.done` event with an `image_generation_call` item and non-empty Base64 `result`. If the raw check fails with a provider-side shape or policy error, the wire changed; if raw succeeds but the product path fails, debug vBot's payload/header/parser path.
