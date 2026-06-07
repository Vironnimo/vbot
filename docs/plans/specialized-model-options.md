## Plan: Per-Model Specialized-Model Options (model-aware schemas + full OpenRouter option set)

**Goal:** Every configured specialized-model target (STT, TTS, image) offers exactly the options the *selected model* supports — including the full Recraft/Sourceful image option set — backed by reliable stored model data, with the Settings UI adapting per selected model and every value forwarded correctly to the provider API. OpenRouter is first-class and complete; OpenAI is a secondary, separable phase.

**Context:**
`option_schema_for` in `core/model_tasks/options.py` branches only on `provider_id` + `task_type` and ignores `model_id`, although `TaskModelService.options(task_type, target)` already parses `model_id` and `self._models` (a `ModelRegistry` with `get(provider_id, model_id)`) is injected. Consequences confirmed by code + live-API research:
- **Image** only sends `aspect_ratio` + `image_size`, OpenRouter-only. OpenRouter actually supports a much larger, **model-specific** option set (see Verified API Facts).
- **TTS** shows 11 hardcoded OpenAI voices for *all* providers — wrong for OpenRouter models. Live `supported_voices` exists per model (kokoro 54, gemini-tts 30, voxtral 30, grok 5, …) but our normalization drops it. This is a live correctness bug.
- **Reliable data** is auto-discoverable for OpenRouter only as `supported_parameters` (→ `seed`) and the top-level `supported_voices` array. The **render hints** for image (aspect-ratio exceptions, `0.5K`, Recraft/Sourceful fields) are **not in any API response** → they are backend-owned authored profiles (the spec already designates `options.py` as the home of render hints).

User decisions captured before planning:
1. OpenRouter is the priority and must be complete; OpenAI is taken along but not the priority.
2. Variant A for **discoverable** data: it lives on the `Model` — OpenRouter via adapter normalization, OpenAI via `resources/models/openai.overrides.json`. Non-discoverable image render hints (family params, aspect-ratio exceptions) are authored in `options.py`, consistent with the "options are backend-owned render hints" convention.
3. **Recraft/Sourceful family image options must be IN** (not deferred) — this is the bulk of "as many options as possible."
4. Image must not stay OpenRouter-only (OpenAI native path = secondary). Video is out.

Key simplification: the Settings panel **already** re-fetches the schema on target/model change ([`loadTaskModelSchema`](../../webui/src/components/settings/SettingsSpecializedModelsPanel.svelte)) and renders fields generically by type. So "UI adapts per model" is mostly a backend concern — *except* that the generic renderer only supports `text/textarea/select/number/boolean`, which cannot express the complex array/object family params. One new generic field type (`json`) closes that gap without adding provider-specific UI rules.

---

## Verified API Facts (researched against the live OpenRouter API + docs, June 2026 — do NOT re-research)

**TTS voices — `/api/v1/models?output_modalities=speech`:**
- Each model entry carries a **top-level** `supported_voices` field (NOT inside `architecture`): a JSON **array of plain voice-id strings**, e.g. `["af_alloy","af_aoede",…]`. Verified counts: `hexgrad/kokoro-82m` 54, `google/gemini-3.1-flash-tts-preview` 30, `mistralai/voxtral-mini-tts-2603` 30, `sesame/csm-1b` 7, `canopylabs/orpheus-3b-0.1-ft` 7, `x-ai/grok-voice-tts-1.0` 5, `zyphra/zonos-*` 5, `microsoft/mai-voice-2` 4. The field is present (often empty) on non-speech models too — read defensively from `raw["supported_voices"]`.
- TTS endpoint `/api/v1/audio/speech`: `model`, `input`, `voice` (required, provider-specific), `response_format` ∈ {`mp3`,`pcm`} (API default `pcm`; vBot keeps `mp3`), `speed`, `provider` passthrough.

**Image — `/api/v1/chat/completions`, `modalities` `["image"]` or `["image","text"]`:**
- `seed` is a **top-level** param (NOT in `image_config`). Present in `supported_parameters` for: all `black-forest-labs/flux.2-*`, `google/gemini-*-image*`, `openai/gpt-*-image*`, `x-ai/grok-imagine-image-quality`. Absent for `recraft/*` (`[]`), `sourceful/*` (`[]`/`['reasoning']`), `bytedance-seed/seedream-4.5`, `microsoft/mai-image-2.5`.
- `image_config.aspect_ratio` (string), default `1:1`. Base set: `1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9`. Exceptions: `microsoft/mai-image-2.5` → only `1:1,4:3,3:4,16:9,9:16,3:2,2:3`; `google/gemini-3.1-flash-image-preview` → base set **plus** `1:4,4:1,1:8,8:1`.
- `image_config.image_size` (string), default `1K`. Values `1K,2K,4K`; `0.5K` **only** on `google/gemini-3.1-flash-image-preview`.
- **Recraft family** (`recraft/*`), all nested under `image_config`:
  - `strength` number 0.0–1.0, default 0.2 — `recraft-v3,recraft-v4,recraft-v4-pro` (img2img).
  - `text_layout` array of `{ text:string, bbox:[[x,y]×4] (0–1) }` — `recraft-v3` only.
  - `style` string — `recraft-v3` only (vector styles unsupported).
  - `rgb_colors` array of `[r,g,b]` (0–255) — `recraft-v3,v4,v4-pro`.
  - `background_rgb_color` `[r,g,b]` (0–255) — `recraft-v3,v4,v4-pro`.
  - (Live catalog also lists `recraft-v4.1*` variants; docs enumerate v3/v4/v4-pro. Author the profile for the documented variants and apply to the matching base names; extend to v4.1 conservatively.)
- **Sourceful family** (`sourceful/riverflow-*`), all nested under `image_config`:
  - `font_inputs` array of `{ font_url:string, text:string }`, max 2 — v2 + v2.5 (+$0.03/input).
  - `super_resolution_references` array of URL strings, max 4 — v2 only, img2img only (+$0.20/ref).
  - `scoring_prompt` string — v2.5 only.
  - `scoring_rubric` array of `{ key,label,description,weight, passing_score?, score_guidance?:[{score,description}] }`, 1–8 entries — v2.5 only.
  - `background_mode` enum `original`(default)`,transparent,solid` — v2.5 only.
  - `background_hex_color` `#RRGGBB` — v2.5 only, required when `background_mode=solid`.
- Field-type mapping to the UI renderer: `strength`→number, `style`/`scoring_prompt`→text/textarea, `background_mode`→select, `background_hex_color`→text. Complex (`text_layout, rgb_colors, background_rgb_color, font_inputs, super_resolution_references, scoring_rubric`)→ new `json` field type.

**OpenAI (secondary):** `/v1/models` is bare (catalog has 1 model) → specialized models + their facts must be authored in `openai.overrides.json`. Images API `/v1/images/generations`: `model,prompt,n,size,quality,background,output_format,style(dall-e-3),response_format`. TTS `/v1/audio/speech`: voices `alloy,ash,ballad,coral,echo,fable,nova,onyx,sage,shimmer,verse`; formats mp3/opus/aac/flac/wav/pcm; `instructions` (gpt-4o-mini-tts). STT `/v1/audio/transcriptions`: language, prompt, response_format, temperature.

---

**Scope:**
- **In:** discoverable model facts on `Model` (OpenRouter `supported_voices`); new generic `json` UI field type; model-aware `options()` resolving the `Model`; complete model-specific schemas for STT/TTS/image incl. Recraft/Sourceful authored profiles and aspect-ratio/image-size exceptions; correct OpenRouter wire shaping (image `image_config` passthrough incl. family fields + top-level `seed`; TTS model-correct voice/format). Secondary: OpenAI overrides + option profiles + native `/v1/images/generations`. Spec updates. Tests alongside each change.
- **Out:** `video_generation`. Mistral speech execution (`NotImplemented`). New local engines. Agent-facing `image_generation`/`text_to_speech` tool schemas (stay prompt/text only). Bespoke per-field widgets for complex types (the `json` field makes them settable now; pretty widgets are a later polish).

**Assumptions & Constraints:**
- Generated catalogs are artifacts; OpenRouter discoverable facts go in normalization, OpenAI facts in `openai.overrides.json`. Image render hints (family params, aspect-ratio exceptions) are authored in `options.py`, not the catalog.
- Schemas remain backend-owned render hints; frontend stays generic (one new generic type, no provider-specific rules).
- Execution domains own final wire payloads; `core/model_tasks/` only selects the binding + emits the schema.
- Newly-captured `supported_voices` needs a catalog refresh (`python cli/main.py model refresh --provider openrouter`, server + key) to appear at runtime; tests use fixtures.
- No legacy/compat branches.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Discoverable facts on Model | `Capabilities.supported_voices` captured by OpenRouter normalization; round-trips through serialization + overrides. |
| M2 | Generic `json` field type | Backend allows `type:"json"`; frontend renders/parses it generically; tests green. |
| M3 | Model-aware complete schema | `options()` resolves the `Model`; STT/TTS/image schemas are model-specific incl. Recraft/Sourceful profiles + aspect-ratio/size exceptions + discovered `seed`; TTS voice bug fixed. |
| M4 | Correct OpenRouter wire | `image_config` carries all present image fields (universal + family); `seed` top-level; TTS sends model voice/format. End-to-end OpenRouter works. |
| M5 (secondary) | OpenAI taken along | OpenAI image/TTS/STT via overrides + profiles + native images path. |
| M6 | Specs | model_tasks/image/speech/models/providers-openrouter specs updated. |

### Phase Breakdown

#### Phase 1: Discoverable model facts (M1)
**Goal:** `Model` carries `supported_voices`; OpenRouter normalization captures it; serialization + overrides round-trip.
**Can run in parallel with:** Phase 2 (different files).

- Add `Capabilities.supported_voices: tuple[str, ...]` with normalization/freeze + (de)serialization in `to_dict`/`from_data`. — read: [.vorch/specs/models.md], files: [core/models/models.py], tests: [tests/core/models/test_models.py]
- OpenRouter `normalize_catalog_entry`: read `raw["supported_voices"]` (top-level, defensive default `[]`) into the capability; keep `supported_parameters`; keep `metadata` provider-namespaced. — read: [.vorch/specs/providers/openrouter.md], files: [core/providers/openrouter.py], tests: [tests/core/providers/test_openrouter.py]
- Confirm `apply_overrides` merges authored `supported_voices`/`supported_parameters` (used by OpenAI in Phase 5); add regression test if a gap exists. — files: [tests/core/models/test_discovery.py]

**Dependencies:** none.
**Done when:** an OpenRouter TTS fixture entry with `supported_voices` round-trips normalize→serialize→`ModelRegistry.get`; `python scripts/quality.py core/models core/providers` green.

#### Phase 2: Generic `json` option field type (M2)
**Goal:** Complex array/object options become settable through one new generic field type.
**Can run in parallel with:** Phase 1.

- Backend: allow `type:"json"` in `TaskModelOptionField` (validation/whitelist only; value passthrough). — files: [core/model_tasks/options.py], tests: [tests/core/model_tasks/test_options.py]
- Frontend renderer: add a `json` branch (multiline textarea bound to a JSON string; parse on input, keep last-valid value, show inline parse error; serialize object/array values for display). Update `valueFromTaskModelOptionField` + `taskModelOptionValue` to stringify/parse JSON values. — read: [.vorch/specs/webui.md], files: [webui/src/components/settings/SettingsSpecializedModelsPanel.svelte, webui/src/lib/taskModelSettings.js], tests: [webui/src/lib/__tests__/taskModelSettings.test.js, webui/src/components/__tests__/SettingsView.test.js]

**Dependencies:** none.
**Done when:** a `json` field renders, accepts `[{"text":"hi","bbox":[[0,0],[1,0],[1,1],[0,1]]}]`, stores the parsed structure, and rejects invalid JSON inline; `python scripts/quality-frontend.py` green.

#### Phase 3: Model-aware complete schemas (M3) — the core phase
**Goal:** `options()` returns full model-specific schemas; the OpenAI-voices-for-everyone bug is gone; Recraft/Sourceful + aspect-ratio/size exceptions + `seed` are present per model.
**Can run in parallel with:** none (depends on Phases 1 & 2).

- Thread the model into the schema: in `TaskModelService.options()`, resolve `self._models.get(provider_id, model_id)` for provider targets, pass the `Model` (capabilities + metadata + model_id) into the builder; fall back to current conservative behavior when not found. — read: [.vorch/specs/model_tasks.md], files: [core/model_tasks/model_tasks.py], tests: [tests/core/model_tasks/test_model_tasks.py]
- Rework `core/model_tasks/options.py` into model-aware builders (one file; tasks below are sequential within it):
  - **TTS:** `voice` select from `model.supported_voices` when non-empty; else fallback (OpenAI list only for `provider_id=="openai"`, free-text `voice` otherwise — never OpenAI's list for others). `response_format` per provider (OpenRouter mp3/pcm; OpenAI full set). Keep `speed`, `instructions`.
  - **Image:** universal `aspect_ratio` (base 10) + `image_size` (1K/2K/4K) with **model-specific overrides** (mai-image-2.5 reduced ratios; gemini-3.1-flash-image-preview extended ratios + `0.5K`); `seed` only when `"seed" ∈ supported_parameters`; **Recraft profile** keyed by `model_id.startswith("recraft/")` (strength/text_layout(json)/style/rgb_colors(json)/background_rgb_color(json), gated by variant); **Sourceful profile** keyed by `model_id.startswith("sourceful/")` (font_inputs(json)/super_resolution_references(json)/scoring_prompt/scoring_rubric(json)/background_mode/background_hex_color, gated by v2 vs v2.5). Use the exact field names/types/values from Verified API Facts.
  - **STT:** keep `language`/`temperature`; `prompt` non-OpenRouter only; add `response_format` when `"response_format" ∈ supported_parameters`.
  - files: [core/model_tasks/options.py], tests: [tests/core/model_tasks/test_options.py]

**Dependencies:** Phases 1, 2.
**Done when:** `options("text_to_speech", kokoro-target)` returns 54 kokoro voices; `options("image_generation", recraft-v3-target)` includes strength+style+text_layout(json)+rgb_colors(json); `options("image_generation", gemini-3.1-flash-image-target)` includes `0.5K` + extended ratios; `options(..., flux-target)` includes `seed`; non-recraft excludes recraft fields. `python scripts/quality.py core/model_tasks` green.

#### Phase 4: OpenRouter wire shaping (M4)
**Goal:** Stored options reach the OpenRouter APIs correctly.
**Can run in parallel with:** image vs speech files don't overlap → the two tasks are ⚡ parallel-safe.

- Image wire (`ProviderImageClient._generate_openrouter`): build `image_config` from the **known image_config keys present in options** (aspect_ratio, image_size, strength, style, rgb_colors, background_rgb_color, text_layout, font_inputs, super_resolution_references, scoring_prompt, scoring_rubric, background_mode, background_hex_color) without inventing absent keys; send top-level `seed` when present; keep the normalized-result contract. ⚡ — read: [.vorch/specs/image.md], files: [core/image/providers.py], tests: [tests/core/image/test_providers.py]
- Speech wire: verify `voice`/`response_format`/`speed` forwarded (largely correct); add coverage that an OpenRouter model-specific voice is forwarded verbatim. ⚡ — read: [.vorch/specs/speech.md], files: [core/speech/providers.py], tests: [tests/core/speech/test_providers.py]

**Dependencies:** Phase 3.
**Done when:** an OpenRouter image request with a Recraft `style`+`rgb_colors` and a Gemini `0.5K`+`seed` sends the correct `image_config`/top-level shape (asserted in tests); an OpenRouter TTS call uses a model-listed voice; `python scripts/quality.py core/image core/speech` green.

#### Phase 5 (secondary): OpenAI taken along (M5)
**Goal:** OpenAI image/TTS/STT targets with correct authored options + working image wire path. Fully separable; can be split into its own plan if descoped.
**Can run in parallel with:** Phase 6.

- `resources/models/openai.overrides.json`: author `gpt-image-1`/`dall-e-3` (image), `tts-1`/`tts-1-hd`/`gpt-4o-mini-tts` (speech, with `supported_voices`), `whisper-1`/`gpt-4o-transcribe`/`gpt-4o-mini-transcribe` (transcription) — task types + `supported_parameters`. — read: [.vorch/specs/models.md, .vorch/specs/model_tasks.md], files: [resources/models/openai.overrides.json], tests: [tests/core/models/test_discovery.py]
- OpenAI option profiles in the builder: image (`size,quality,background,n,output_format,style`), TTS (full format set + `instructions` for gpt-4o-mini-tts). — files: [core/model_tasks/options.py], tests: [tests/core/model_tasks/test_options.py]
- OpenAI native image wire: add `/v1/images/generations` handling in `ProviderImageClient` returning normalized `ImageGenerationResult`; map `n>1` to multiple artifacts (loop already supports it). — read: [.vorch/specs/image.md], files: [core/image/providers.py], tests: [tests/core/image/test_providers.py]

**Dependencies:** Phases 1–4.
**Done when:** an OpenAI image target appears with size/quality/background/n and produces artifacts via `/v1/images/generations`; OpenAI TTS shows the OpenAI voices; tests green.

#### Phase 6: Specs (M6)
**Goal:** Specs match shipped behavior.
**Can run in parallel with:** Phase 5.

- Update: `model_tasks.md` (options model-aware via `ModelRegistry.get`; `json` field type; render-hint contract), `image.md` (image_config passthrough incl. family fields, `seed`, model-specific ratios/sizes; OpenAI native path if Phase 5 lands), `speech.md` (model-specific voices/formats), `models.md` (`supported_voices`), `providers/openrouter.md` (voices capture). — files: [.vorch/specs/model_tasks.md, .vorch/specs/image.md, .vorch/specs/speech.md, .vorch/specs/models.md, .vorch/specs/providers/openrouter.md]

**Dependencies:** lands after the code it documents.
**Done when:** each touched spec matches behavior.

### Open Decisions

- **OD1 — Voices field home.** **Default (chosen):** `Capabilities.supported_voices: tuple[str,...]` — uniform read across providers, mirrors `supported_parameters`. Alternative: `metadata.openrouter.supported_voices` (provider-branchy). No further input needed unless you object.
- **OD2 — RESOLVED by user:** Recraft/Sourceful family options are **in** (Phase 3), made settable via the `json` field type for complex params.
- **OD3 — Complex-type UX.** `json` textarea now (every option settable) vs bespoke repeatable widgets (nicer, much larger frontend scope). **Default:** `json` now, bespoke widgets as optional later polish. Flag only because it affects how friendly the Recraft/Sourceful UI feels on first ship.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **R1 — RESOLVED:** `supported_voices` confirmed present (top-level array of strings) on the live `/models?output_modalities=speech` response. | — | — | Verified; Phase 1 reads `raw["supported_voices"]`. |
| `image_config` family fields rejected by a model that doesn't support them. | Med | Low | Schema only exposes a field for the matching model/family; wire sends only keys present in options. Provider 4xx already surfaces as `ImageExecutionError`. |
| `json` field lets users submit malformed structures. | Med | Low | Frontend inline JSON validation + keep-last-valid; backend passes through; provider validates. |
| Newly-captured voices need a catalog refresh to appear at runtime. | High | Low | Operational step documented; tests use fixtures. |
| Recraft `v4.1*` variants not in docs' param lists. | Low | Low | Author for documented variants; apply by base-name match; extend conservatively. |
| OpenAI overrides drift from real API constants. | Low | Med (secondary) | Author from current OpenAI docs; Phase 5 only. |

**Final size:** **Large** (grew from Medium during research: a new UI field type + authored complex-type image profiles + model-specific value sets). OpenRouter path (M1–M4) is the shippable core; OpenAI (M5) is a separable tail.
