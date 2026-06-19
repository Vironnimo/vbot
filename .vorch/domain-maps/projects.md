# Projects

Owns the Project entity, its `project.json` schema, and the data-dir **anchor** lifecycle. A Project is a first-class entity (GLOSSARY → Project), not a bare cwd.

## Overview

`core/projects/` is one deep module. It owns:

- The **Project entity** and `project.json` shape (`projects.py`).
- **cwd normalization, the duplicate-cwd identity key, and slug derivation** for both project and agent ids (`paths.py`).
- The **anchor lifecycle** in the data-dir: layout, CRUD, archive-on-remove (`store.py`).
- The **pluggable scanner** (`scanners/`), the **scan report** (`scan_report.py`), and **uniform agent resolution + the model chain** (`resolver.py`) — see Scanning & Resolution below.

It does **not** own field validation — that lives in the central settings validator (`core/settings/validation.py`), the same way Agents and Channels validate. The `project_id` backbone through sessions/runs/tools/chat is owned by those domains (see their maps); this module only feeds the project anchor path and cwd into them. The project RPC/CLI surface is not here yet (Phase 5).

The anchor holds **no run config** — only Sessions ownership and the local agent id. An agent's config comes live from the scan/repo, never from the anchor (design decision #4).

## Data Model

**Project** (frozen dataclass, `projects.py`): `project_id` (stable slug key), `display_name` (changeable), `cwd` (the repo dir tools resolve against — stored in the file, *not* the directory name, so the repo can move without breaking the key or Sessions), `default_agent`, `default_model` (optional pointers; empty string = fall through the resolution chain), `auto_load` (ordered file list; `AGENTS.md` is seeded as its first entry at creation — see Interfaces), `created_at`/`updated_at` (UTC ISO 8601, `Z` suffix). The minimal valid Project is just a cwd — team and auto-load are all optional (`AGENTS.md` is seeded into `auto_load` at creation but stays a normal removable entry).

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

- `create(project_id, display_name, cwd, *, default_agent, default_model, auto_load)` → `Project`. Rejects a duplicate id and a cwd already claimed by another project. The cwd folder need not exist yet (a bare/missing repo is an open-time concern, not a create-time one). **Seeds `AGENTS.md` as the first `auto_load` entry** (`seed_default_auto_load` in `projects.py` — case-insensitive, idempotent): creation-only, so `update` never re-seeds and a user's removal sticks. Unconditional (seeded even when no `AGENTS.md` exists on disk yet) — rendering is lazy, so a not-yet-present entry costs nothing and loads the moment the file appears.
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

## Scanning & Resolution

**Scanner** (`scanners/`) — pluggable, one detector per format, the same seam shape as provider adapters (a new format is a new detector, not a rewrite). `AgentDetector` (`Protocol`) exposes `format_key` + `detect(project_root) -> list[DetectedFile]`; the registry carries an explicit format **rank** (OpenCode = 0). `scan_project(project_root, *, registry=None) -> ScanResult(team, report)` runs each detector at its known location — **non-recursive** (OpenCode reads only `.opencode/agents/` at the project root, never a tree walk), so nested repos are not swept in. The detector parses its format into one internal **`ScannedAgent`** profile (`agent_id` slug, `display_name`, `description`, raw `model` 1:1, `temperature`, verbatim `body`, `tools=("*",)`, `skills=("*",)`, `source_format`, `source_path`); the resolver maps that to the runtime agent. This is the seam for *all* agent formats — a future identity-bearing format adds a detector that also produces a workspace.

**Scan report** (`scan_report.py`) — collects only what is *unclean under what exists* (an empty folder / no team / no AGENTS.md is normal → clean empty report). `FindingType`: `SLUG_COLLISION`, `UNSLUGIFIABLE_NAME`, `BAD_MODEL`, `ORPHAN`. **Collision is deterministic:** two files on one `agent_id` are resolved by `(rank, filename)` — format precedence first, then stable by filename, **never filesystem order** (Windows ≠ Linux); the loser becomes a `SLUG_COLLISION` finding. `BAD_MODEL` findings are fed in by the resolver (`with_model_findings`) and pointer/orphan findings by the anchor (`with_pointer_findings`) — the structural findings (collision, unslugifiable) the scan produces itself.

**Resolver** (`resolver.py`) — `AgentResolver.resolve_agent(project_id, agent_id) -> RuntimeAgent` is the single run-path seam (details and the model chain in `agent.md` → Uniform Agent Resolution). It holds a per-project team-scan cache (`rescan_project` / `invalidate_team_cache`), reads each agent's config fresh per resolve, and runs the model chain with `ModelConfigurationChecker` (provider registered + model in catalog + usable credential). `scan_project_report(project)` returns team + the full report including `BAD_MODEL` findings. Wired into the runtime as `runtime.agent_resolver` (built from `agents`, `projects`, `models`, providers, credentials); the resolver imports `core.runtime` not at all — it depends on small local `Protocol`s.

## Scope boundary (what is *not* here yet)

The project RPC surface (`project.*`) and the CLI `project` area are Phase 5 and live in `server/` and `cli/`, not here. Channels on project agents and a project-local memory tool are deferred.
