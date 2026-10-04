# Generated Media Tools

Task-gated reference for the Agent-facing `generate_video` and `generate_music` Tools.

## Contract

Both Tools are always registered and use a configured Task Model at execution time. They have flat open provider schemas whose unknown arguments fail at dispatch, structured success results with an absolute model-facing path, media type, and byte size, and no implicit provider fallback. Music results add the audio `transcript` and `model_reply` (its text beside the audio) when non-empty. Definition Profiles are derived from stable configured-Model facts: `generate_video` builds its schema from `VideoService.generation_profile()` (`generate_video_parameters`, `model_tasks/media-profiles.md`): `duration` as an integer enum, or `minimum`/`maximum` for contiguous seconds; `aspect_ratio` and `resolution` as string enums; `generate_audio` and the frame positions only when offered; `size` and `seed` stay in Settings. `generate_music` exposes `source_images` only for a Model with image input.

Local frame/reference files are resolved against the Run's effective cwd and uploaded only when the Agent supplies the corresponding argument. They go through the shared local image checks of the image Tools (`core/tools/_image_inputs.py`, see `tools/image.md`): `file:` URLs become paths, web and `data:` addresses fail with `invalid_arguments`, folders fail with `image_read_error` and an example, and missing files fail with `image_not_found` naming similar files and the corrected call (`first_frame`/`last_frame` render it as one path string). Output directories use the caller-owned generated-media policy: an explicit path wins; otherwise Identity and Rooted calls use `<Workspace>/<kind>-gen/`, while Project Agent calls use `<effective cwd>/<kind>-gen/`; the `output_dir` descriptions name the default `video-gen`/`music-gen` folder, and missing folders are created. Files use UUID names and exclusive creation so existing workspace files are never overwritten.

Dialects (owner argument normalizers): `start_frame`, `start_image`, `first_frame_image` -> `first_frame`; `end_frame`, `end_image`, `last_frame_image` -> `last_frame`; `source_image`, `input_image(s)`, `reference_image(s)`, `image_path(s)` -> `source_images` (one string or path object becomes a list); `output_directory`, `out_dir`, `save_dir`, `output_folder` -> `output_dir`; a `duration` of `"6s"` or `"6 seconds"` -> `6`. Empty optional strings (`output_dir`, frames, `resolution`, `aspect_ratio`) count as omitted, and a whitespace-only `output_dir` uses the default folder.

Requested frame and reference images are recorded via `ToolContext.add_display_media` as sources, and a successful call records the written file, so expanded details show the sources and a video or audio player (see `tools.md`).

Failures keep their Task codes (`_media_failure`), worded by `core/tools/_media_failures.py` (`tools/image.md` -> Agent-facing text): a refusal is `generation_refused` with the shared refusal wording; a video job that was accepted but did not finish or download is `provider_outcome_unknown` with the job-id wording; other unknown outcomes use the shared outcome-unknown wording; `provider_error` uses the shared Provider wording; a configuration error uses the shared unavailable wording that names the Settings place; an unoffered per-call choice or frame, and `source_images` for a Music Model without image input (`MusicOptionError`), is `invalid_arguments` with a message that starts `Nothing was generated.` and names the valid values or the field to omit; output folder and save failures use `output_failure_message`. All of them set `retryable: false` except retryable Provider errors. A missing `prompt` fails with an example prompt.

## Ownership

Tool definitions, display metadata, result shaping, failure projection, and registration live together in `core/tools/media_generation.py`; argument dialects and local image checks come from `core/tools/_image_inputs.py`. Tests: `tests/core/tools/test_media_generation.py`. Task-specific capability validation, external requests, and artifact writes remain in `core/model_tasks`; do not duplicate them in Tool handlers.

## Agent-facing text

`core/tools/media_generation.py` descriptions and parameters (failure wording: `tools/image.md` -> Agent-facing text):

| Text | Reason |
|---|---|
| `Generate a video from a text prompt with the configured model and save it as a local file.` / `Generate a music track ...` | Names the outcome, a saved file, so the Agent reports the path instead of describing an unseen video or track. |
| `Each call is billed by the provider.` | Hypothesis without Session evidence (no real call of either Tool up to 2026-10-04): nothing else tells the Agent that a call costs money, so it could generate variants nobody asked for. A black-box task that invites variants would confirm it. |
| `An image passed as first_frame is uploaded to the provider.` / `Images passed as first_frame or last_frame ...` / `Images passed as source_images ...` | Local files leave the machine; the Agent must know before it passes a private image. |
| `source_images` (music): `Local images to use as references for the track. ... Omit to generate from the prompt alone.` | Role and omit rule; the earlier `Optional ...` gave no decision. |
| `prompt` (video): `Describe the video: subject, action, setting, camera movement, and style.` | Names what a video prompt needs beyond a subject: action and camera movement. |
| `prompt` (music): `Describe the music: style, mood, instrumentation, structure, and any lyrics.` | Names what a music prompt needs; `any lyrics` tells the Agent that lyrics go in the prompt. |
| `first_frame`: `Local image to start the video with. Relative paths start at the working directory. Omit to start from the prompt alone.` / `last_frame`: `Local image to end the video with. ... Omit to let the model choose the ending.` | Role, path base and an omit rule; without the omit rule weak Models fill every field. |
| `output_dir`: `Folder for the generated video, created if missing; relative paths start at the working directory. Omit to use the default video-gen folder.` (music: `music`, `music-gen`) | Weak Models fill `output_dir`; the sentence says omission is fine and where files go. |
| `duration`: `Length in seconds. Omit to use the default.` | The schema carries the offered seconds; the unit is not visible from an integer enum. |
| `aspect_ratio`, `resolution`: as in `image_generation` | One wording for the same choice across media Tools. |
| `generate_audio`: `true to generate audio with the video, false for a silent video. Omit to use the default.` | States what each value produces. `audio` covers speech and effects; `soundtrack` read as music only. |
