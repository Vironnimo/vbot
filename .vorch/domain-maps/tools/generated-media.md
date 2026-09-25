# Generated Media Tools

Task-gated reference for the Agent-facing `generate_video` and `generate_music` Tools.

## Contract

Both Tools are always registered and use a configured Task Model at execution time. They have flat open provider schemas whose unknown arguments fail at dispatch, structured success results with an absolute model-facing path, media type, and byte size, and no implicit provider fallback. Definition Profiles are derived from stable configured-Model facts: `generate_video` exposes only catalog-supported native controls and frame positions, while `generate_music` exposes `source_images` only for a Model with image input.

Local frame/reference files are resolved against the Run's effective cwd and uploaded only when the Agent supplies the corresponding argument. They go through the shared local image checks of the image Tools (`core/tools/_image_inputs.py`, see `tools/image.md`): `file:` URLs become paths, web and `data:` addresses fail with `invalid_arguments`, folders fail with `image_read_error` and an example, and missing files fail with `image_not_found` naming similar files and the corrected call (`first_frame`/`last_frame` render it as one path string). Output directories use the caller-owned generated-media policy: an explicit path wins; otherwise Identity and Rooted calls use `<Workspace>/<kind>-gen/`, while Project Agent calls use `<effective cwd>/<kind>-gen/`; the `output_dir` descriptions name the default `video-gen`/`music-gen` folder, and missing folders are created. Files use UUID names and exclusive creation so existing workspace files are never overwritten.

Dialects (owner argument normalizers): `start_frame`, `start_image`, `first_frame_image` -> `first_frame`; `end_frame`, `end_image`, `last_frame_image` -> `last_frame`; `source_image`, `input_image(s)`, `reference_image(s)`, `image_path(s)` -> `source_images` (one string or path object becomes a list); `output_directory`, `out_dir`, `save_dir`, `output_folder` -> `output_dir`. Empty optional strings (`output_dir`, frames, `resolution`, `aspect_ratio`, `size`) count as omitted, and a whitespace-only `output_dir` uses the default folder.

Failures keep their Task codes. `provider_error` messages use the shared Provider wording of `core/tools/_media_failures.py` (credentials, rate or usage limit, no answer, other refusal; see `tools/image.md`). Configuration errors pass through unchanged because they also carry request fixes (`duration must be one of the values supported by the configured model: ...`), and `provider_outcome_unknown` keeps its message with `retryable: false`. A missing `prompt` fails with an example prompt.

## Ownership

Tool definitions, display metadata, result shaping, failure projection, and registration live together in `core/tools/media_generation.py`; argument dialects and local image checks come from `core/tools/_image_inputs.py`. Tests: `tests/core/tools/test_media_generation.py`. Task-specific capability validation, external requests, and artifact writes remain in `core/model_tasks`; do not duplicate them in Tool handlers.

