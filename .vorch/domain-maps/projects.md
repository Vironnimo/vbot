# Projects

Owns the Project entity, its `project.json` schema, and the data-dir **anchor** lifecycle. A Project is a first-class entity (GLOSSARY → Project), not a bare cwd.

## Overview

`core/projects/` is one deep module. It owns:

- The **Project entity** and `project.json` shape (`projects.py`).
- **cwd normalization, the duplicate-cwd identity key, and slug derivation** (`paths.py`).
- The **anchor lifecycle** in the data-dir: layout, CRUD, archive-on-remove (`store.py`).

It does **not** own field validation — that lives in the central settings validator (`core/settings/validation.py`), the same way Agents and Channels validate. It does not own scanning, team discovery, agent resolution, or the model chain (Phase 3, future `scanners/` subpackage + `resolver.py`), nor the `project_id` backbone through sessions/runs/tools (Phase 2).

The anchor holds **no run config** — only Sessions ownership and the local agent id. An agent's config comes live from the scan/repo, never from the anchor (design decision #4).

## Data Model

**Project** (frozen dataclass, `projects.py`): `project_id` (stable slug key), `display_name` (changeable), `cwd` (the repo dir tools resolve against — stored in the file, *not* the directory name, so the repo can move without breaking the key or Sessions), `default_agent`, `default_model` (optional pointers; empty string = fall through the resolution chain), `auto_load` (ordered file list), `created_at`/`updated_at` (UTC ISO 8601, `Z` suffix). The minimal valid Project is just a cwd — team, AGENTS.md, and auto-load are all optional.

**Anchor layout** in the data-dir (never in the repo):

```
<data_dir>/projects/<project-id>/
    project.json                 ← the entity above
    agents/<agent-id>/
        sessions/                ← project-scoped session ownership (Phase 2 writes here)
        workspace/               ← only for a rooted identity agent; config agents never have one
```

`<project-id>` is the directory name and the stable key; the cwd path lives inside `project.json`. Identity agents stay separate under the existing top-level `agents/` — untouched.

## Interfaces

`ProjectStore(data_dir)` — CRUD over anchors:

- `create(project_id, display_name, cwd, *, default_agent, default_model, auto_load)` → `Project`. Rejects a duplicate id and a cwd already claimed by another project. The cwd folder need not exist yet (a bare/missing repo is an open-time concern, not a create-time one).
- `get(project_id)` → `Project` (raises `ProjectNotFoundError`); `exists(project_id)` → bool.
- `list()` → `list[Project]` sorted by id; a single corrupt `project.json` is skipped with a logged warning rather than aborting the listing.
- `update(project_id, **changes)` → `Project`. `project_id` is immutable (passing it is an "unknown field" error). Changing `cwd` re-normalizes and re-checks the duplicate guard. Rebuilds through `build_project` so there is one validation path.
- `delete(project_id)` → archive `Path`. Archives the subtree, does not hard-delete.
- `sessions_dir(project_id, agent_id)` / `workspace_dir(project_id, agent_id)` → the per-agent anchor paths Phase 2 consumes.

`paths.py` helpers (the cwd contract):

- `normalize_cwd(cwd)` → `Path`: the **stored, display-facing** absolute cwd — symlinks and `.`/`..` resolved (`os.path.realpath`), trailing separator dropped, **case preserved**. This is what tools resolve against and what the user sees.
- `cwd_identity_key(cwd)` → str: the **comparison key** for duplicate detection. Same resolution as above, then case-folded **only on Windows** (`os.name == "nt"`, NTFS is case-insensitive); POSIX stays case-sensitive (`/srv/A` ≠ `/srv/a`).
- `cwd_exists(cwd)` → bool: whether the cwd currently resolves to a directory (drives the "repo not found → re-point" product behavior).
- `slugify_project_id(display_name)` → str: lowercase → NFKD transliterate/strip non-ASCII → non-`[a-z0-9_-]` runs → single hyphen → trim edge separators → truncate to 64. Raises `ValueError` when nothing slug-worthy remains (caller surfaces as a "not slugifiable" finding).

**Validation seam** (`core/settings/`): `validate_project_data` / `validate_project_file` / `load_validated_project_json` enforce field rules fail-fast with JSON-path diagnostics, mirroring `validate_agent_data` / `validate_channel_data`. `validate_data_dir_config` (the `vbot doctor config` sweep) now also collects `projects/*/project.json`. The store reads through `load_validated_project_json` and maps `SettingsValidationError` → `ProjectError`. The canonical slug rule lives once in `core/settings/settings.py` as `PROJECT_ID_PATTERN` / `is_valid_project_id` (alongside `AGENT_ID_PATTERN`) — placed there, not in `core/projects/`, to avoid an import cycle (`validation.py` needs the pattern; `core.projects` imports from `core.settings`).

## Conventions

- One validation path: both `create` and `update` go through `build_project`, which normalizes the cwd and validates fields; the store never hand-rolls a second check.
- `project.json` is written atomically (temp file + `os.replace`).
- Errors are a small hierarchy on `ProjectError(ValueError)`: `ProjectAlreadyExistsError`, `ProjectNotFoundError`, `InvalidProjectIdError`.

## Constraints & Gotchas

- **cwd normalization is host-explicit, not host-default.** Deployment is Linux, dev is Windows — duplicate detection must agree across both. Stored cwd preserves case; only the identity key folds case, and only on Windows. Changing this risks either false duplicates on POSIX or missed duplicates on Windows.
- **Removal archives, it does not delete.** `delete` mirrors `AgentStore.delete`: `shutil.move` into `<data_dir>/archive/projects/<project-id>/`, replacing any existing archive for the same id (own `projects/` subtree so a project id can never collide with an agent id in the archive namespace). **The repo (cwd) is never touched** — removing a project is not deleting a repo.
- **The cwd folder is not validated for existence at create/validate time** — a project key is the slug, not the path, so a moved/missing repo is detected later (`cwd_exists`) and offered a re-point, without losing the project.
- **`project_id` is immutable.** It is the anchor directory name; `update` rejects it as an unknown field rather than silently moving the anchor.

## Scope boundary (what is *not* here yet)

Phase 1 only. The `project_id` backbone through Sessions/Runs/Tool-Context (Phase 2), the pluggable scanner + OpenCode detector + scan report (Phase 3, `core/projects/scanners/`), uniform agent resolution + the model chain (Phase 3, `resolver.py`), and the RPC/CLI surface (Phase 5) are not in this module yet. Update this map as each lands.
