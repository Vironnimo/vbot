# Local Engines

Read this reference when you change what every local engine shares: the installation job (`LocalSetup`), managed environments, pinned Model files and downloads, or the generic local engine RPCs. Engine-specific setup stays in `model_tasks/speech.md` and `model_tasks/embeddings.md` -> Local Engine.

## Installation job

`local_setup.py` holds `LocalSetup`, the shared fixed-recipe installation job of every local engine; local speech (`model_tasks/speech.md`) and local embeddings (`model_tasks/embeddings.md` -> Local Engine) extend it. It owns:

- state/phase/error/progress status, `cancel()`, completion receipts and the install lock;
- `activity()`, its in-memory report for background activity: running, completed after an install in this process, failed, or restart due; nothing after a cancel (`server/operator-surfaces.md` -> Background activity);
- uv-managed environments from `[tool.vbot.*]` recipes, each on one required Python `major.minor`: its recipe's `python`, else the server's (also for managed STT, which has no recipe), so environments follow vBot's Python without pins of their own. An environment whose `pyvenv.cfg` names another version reads `python_changed` and the next install recreates it, removing the old tree off the Event Loop (a recipe that cannot be read reads `setup_unavailable`).

A recipe environment is based on a uv-managed Python: setup runs the uv executable directly, because `python -m uv` names the server's interpreter as uv's parent, which uv prefers over `--managed-python` when the versions match, and the environment would then depend on the release's runtime (managed STT, created from the packaged runtime on purpose, is the exception, `model_tasks/speech.md`). `SETUP_TIMEOUT_S` bounds each package or verification command, not the whole job.

Command cleanup settles process-tree termination and reaping before releasing the install lock, including repeated cancel requests or shutdown during cancellation. A queued installation cannot start while the previous command still runs.

A setup may own one pinned Model (`model=PinnedModel`, `models_dir`): its files live in `<models_dir>/<name>/<revision>/`, the target is available only when its environment is ready and that folder holds the receipt `verified.json` (`{repo, revision}`; else `model_missing`), `_fetch_model()` removes the receipt and fetches the files in phase `downloading` with byte `progress`, and `_publish_model()` removes the Model's other revision folders, then writes the receipt. Both tree removals (an old environment, other revisions) and downloads use the named bounded `local-setup` pool. Admitted work settles through repeated cancellation before the install lock releases; downloads receive their cooperative cancellation Event immediately. A cancel while publishing finishes removal but writes no receipt, so the Model stays `model_missing`.

## Pinned Model files

`model_files.py` is the one owner of pinned Model files for every local engine: `ModelFile(path, sha256, size)`, `PinnedModel(repo, revision, files)` (`download_bytes` is their sum) and `fetch_model_files(model, directory, *, progress, cancelled, reuse)`, a blocking call `_fetch_model()` runs in an executor thread. Per file it:

1. keeps a file whose size and SHA-256 already match;
2. else adopts a verified copy from a `reuse` folder (`_fetch_model()` passes the Model's other installed revisions, so a revision bump fetches only changed files) or the user's Hugging Face cache (`HF_HUB_CACHE`, else `HF_HOME/hub`, else `~/.cache/huggingface/hub`; `blobs/<sha256>` or `snapshots/*/<path>`; hard link, else `Path.copy`, which clones blocks where the file system can);
3. else downloads `https://huggingface.co/{repo}/resolve/{revision}/{path}` with httpx and the shared SSL context into `<path>.part`, resuming it with `Range` and retrying 429/5xx and transport errors (4 attempts).

A file takes its final name only after its size and SHA-256 match. Before anything is fetched, the missing bytes plus a 512 MB margin must be free. Error codes: `insufficient_space`, `checksum_mismatch`, `download_failed`; cancelling the job sets a `threading.Event`, the thread stops within one chunk (`interrupted`) and `.part` files stay for the next install. Engines never download at use: a ready target runs offline.

## Generic local engine RPCs

`server/rpc/task_model_methods.py` serves every local target, whichever service runs it (`_LOCAL_OWNERS`: speech, embeddings; each owner's `local_setup_for(target)` raises `ValueError` for targets it does not run). The RPCs accept only the listed params, never commands, packages or paths; a missing, non-`local/` or unknown target is `invalid_request`.

- `task_model.local_setup_status {target}` and `task_model.local_setup_install {target}` -> `{state, phase, error, progress?, restart_available}`. `state` is `missing|installing|ready|failed|restart_required`; `error` a stable code (`""` when none); `progress {completed, total}` (bytes) appears only while installing and counting. Install starts the fixed job, or joins a running one, and returns at once; clients poll status.
- `task_model.local_setup_cancel {target}` -> the same shape, once a running installation has stopped (`LocalSetup.cancel`). A cancelled installation is not a failure: the target reads `missing` again, the next queued installation starts, and verified Model files and partial `.part` downloads stay for the next install. Closing the server still ends a running installation as `failed`/`interrupted`.
- `task_model.local_memory_status {}` -> `{models: [{target, label, loaded, busy}]}` over all local speech and embedding Models.
- `task_model.local_unload {target}` -> the same list plus `released`; it never stops a busy Model.

The WebUI installs every local target through the generic status/install pair (`settings/localSetupJob.svelte.js`, `webui/settings.md`). The older `speech.local_memory_status` and `speech.local_unload` RPCs stay for the current WebUI; `speech.local_setup_restart {target}` (a required speech target, unknown -> `invalid_request`) has no generic counterpart and restarts the server after a development checkout's STT install (`model_tasks/speech.md`). There are no `speech.local_setup_status/install` RPCs.

## Source and tests

- `core/model_tasks/local_setup.py`, `core/model_tasks/model_files.py`; RPCs in `server/rpc/task_model_methods.py`
- `tests/core/model_tasks/test_model_files.py` covers the fetch; `tests/core/model_tasks/conftest.py` replaces the seam `model_files.open_client` with a guard that fails any test that would reach Hugging Face. RPC coverage: `tests/server/rpc/test_task_model_methods.py`.
