# Media Profiles

Task-gated reference for what an image or video generation target offers: `ImageProfile` (`core/model_tasks/image_profile.py`) and `VideoProfile` (`core/model_tasks/video.py`).

## Why profiles

The Model DB publishes raw parameter facts per Model (`capabilities.task_options.<task>`, see `models.md`). One Model behaves differently per wire: the ChatGPT subscription renders through a carrier Model that rejects `n`, and OpenAI's native API takes pixel sizes where OpenRouter takes aspect ratios and resolution tiers. A profile combines the facts with the wire once. Settings fields, the Tool schema and the request translation all read the same profile, so they cannot disagree. A profile never invents values: a Model without published facts offers no per-call choices.

## Image wires

`image_wire(provider_id, adapter, connection_mode)` picks the wire:
- `openrouter` for Provider `openrouter`;
- `openai_subscription` for Adapter `openai`/`openai_compatible` with Connection mode `codex_responses`;
- `openai_images` for every other `openai`/`openai_compatible` Connection;
- `unsupported` otherwise.

`TaskModelService.image_profile(target_ref)` builds the profile from the resolved Model and the target Connection's wire; local targets get `unsupported`. `ImageService.generation_profile()` returns the profile of the configured binding, or `ImageProfile(wire="unsupported")` when the binding cannot run.

## ImageProfile

- `parameters`: the Settings-facing parameter specs after the wire rules.
  - Hidden per wire: OpenRouter `input_references`, `stream`; OpenAI images `response_format`, `stream`; subscription `n`, `response_format`, `stream`, `model`.
  - An open `boolean` spec (OpenRouter's marker for "supported, no values published") becomes `number` for `seed` and `string` otherwise.
- `fixed_options`: wire options sent over any Setting. `openai_images` sends `response_format: b64_json` when the Model lists `response_format`, because DALL-E otherwise answers with URLs.
- `max_source_images`: `0` for a Model without `image` input or an unresolved Model; the published `input_references.max`; `16` on `openai_images` (OpenAI images API reference, read 2026-10-04); else `None` (no known limit). The service refuses more images with `ImageTooLargeError`; the Tool sets `maxItems`, or drops `source_images` at `0`.
- `call_choices`: the per-call options of the image Tool (`CALL_OPTION_NAMES`: `aspect_ratio`, `resolution`, `background`). An option needs at least two values; otherwise the Tool omits it.
  - OpenRouter: the Model's `aspect_ratio` and `resolution` enum values without `auto`.
  - Other wires with a free-form `size` (`type: string`, gpt-image-2): `size_translation = "flexible"`. Aspect ratios `1:1 3:2 2:3 4:3 3:4 16:9 9:16 21:9`, tiers `1K 2K 4K` (pixel budgets). `flexible_size()` returns `WIDTHxHEIGHT` with edges that are multiples of the reduced ratio times 16, at most 3840 px per edge, and 655,360 to 8,294,400 pixels (OpenAI image generation guide, read 2026-10-04). A call that names only one half takes the other from the configured `size` (nearest ratio, nearest tier), else `1:1` or `1K`. Settings show `size` as presets: `auto`, `1024x1024`, `1536x1024`, `1024x1536`, plus the 2K and 4K sizes of `1:1 3:2 2:3 16:9 9:16`.
  - Other wires with an enum `size` (DALL-E, gpt-image-1): `size_translation = "fixed"`. Each size is labelled by the nearest common ratio within 5%, else its exact ratio; the largest size per label wins. DALL-E 3 offers `1:1`, `16:9`, `9:16`. There is no `resolution` choice.
  - `background`: `transparent` and `opaque` when the Model lists `transparent`.
- `wire_options(call_options, binding_options)` translates per-call intent. Values match tolerant of case, spaces and `x`/`/` separators (`match_choice`). An option the profile lacks raises `missing_choice_message` (`Nothing was generated. The configured image model has no {name} choice. Repeat the call without {name}.`); an unoffered value raises `unoffered_choice_message` (`... does not offer {name} {value!r}. Pass one of ..., or omit {name} to use the default.`). `ImageService` raises both as `ImageOptionError` (`invalid_arguments`), before any request. With a size translation, `aspect_ratio`/`resolution` become one `size`.
- `ImageService.generate()` sends `{**binding options, **fixed_options, **wire_options}` (per call > Settings > Provider default) with the prompt unchanged. On the subscription wire the Codex client also writes `size`, `quality` and `background` into the carrier text (`model_tasks/image.md` -> Provider Wire Behavior).
- Settings fields (`_image_options.py`) render `parameters`: enum -> select with a leading Provider default choice, range -> bounded number, number/string -> free field, `provider_options` when passthrough keys are published. An OpenAI-wire Model without facts gets a conservative gpt-image field set (no `n` on the subscription); other Models without facts get no generated fields.

## VideoProfile

- `build_video_profile(model)` reads `task_options.video_generation`. `call_choices` holds `duration`, `aspect_ratio` and `resolution` with at least two values each, `auto` dropped; durations keep only whole seconds, sorted numerically, as text. `generate_audio` is true when the Model publishes that parameter. `frame_images` lists `first_frame`/`last_frame` among the published frame positions.
- `validated_options()` accepts `6s` and `9x16`, sends `duration` as an integer and `generate_audio` as a boolean, and raises `VideoOptionError` (`invalid_arguments`) with the image wording ("video model").
- A requested `aspect_ratio` or `resolution` drops the Settings `size`, which would otherwise override it on the wire; the half the call leaves out is taken from that size when the Model offers it (`1280x720` -> `16:9`, `720p`; a short edge of 2160 or more -> `4K`), so an omitted choice keeps its configured value (`_shape_options`). `size` and `seed` are Settings-only.
- The Tool shows `duration` as an integer enum, or as `minimum`/`maximum` when the seconds are contiguous; `aspect_ratio` and `resolution` as string enums.

## Target filters

`model_supports_task()` also decides target lists (`_provider_targets` applies it):
- Media generation excludes router Models (`openrouter/` prefix, for example `openrouter/auto`): they pick another Model per request and have no fixed facts.
- `video_generation` needs a published `duration` parameter. Models without one edit or upscale videos or animate avatars and cannot run from a prompt.
- `image_understanding` excludes Music Models, which read images but answer with a track.

A Provider with more than one Connection names the Connection in every target label, even when only one is usable, because the Connection decides the wire.

## Tests

`tests/core/model_tasks/test_image_profile.py`, `test_video.py`, `test_model_task_targets.py`; Tool schemas in `tests/core/tools/test_image.py` and `test_media_generation.py`.
