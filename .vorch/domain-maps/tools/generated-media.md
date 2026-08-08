# Generated Media Tools

Task-gated reference for the Agent-facing `generate_video` and `generate_music` Tools.

## Contract

Both Tools are always registered and use a configured Task Model at execution time. They have flat open provider schemas plus handler-level unknown-argument rejection, structured success results with an absolute model-facing path, media type, and byte size, and no implicit provider fallback. Definition Profiles are derived from stable configured-Model facts: `generate_video` exposes only catalog-supported native controls and frame positions, while `generate_music` exposes `source_images` only for a Model with image input.

Local frame/reference files are resolved against the Run's effective cwd and uploaded only when the Agent supplies the corresponding argument. Output directories use the caller-owned generated-media policy: an explicit path wins; otherwise Identity and Rooted calls use `<Workspace>/<kind>-gen/`, while Project Agent calls use `<effective cwd>/<kind>-gen/`. Files use UUID names and exclusive creation so existing workspace files are never overwritten.

## Ownership

Tool definitions, path argument parsing, display metadata, result shaping, and registration live together in `core/tools/media_generation.py`. Task-specific capability validation, external requests, and artifact writes remain in `core/model_tasks`; do not duplicate them in Tool handlers.

