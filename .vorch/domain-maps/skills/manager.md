# Skill Manager Inventory

Task-gated reference for the human Skill manager's read model: `skill.inventory` (`SkillRuntime.skill_inventory()`), its per-Agent and per-Project access projections, the Project Skill pool behind `project.show`, and the events that invalidate them. Read it when changing those payloads or a client that consumes them. Semantics of Agent Skill Exclusions, Disabled and Shared Skills live in `skills.md` -> Terms.

## Inventory shape

`skill.inventory` returns `SkillRuntime.skill_inventory()` unchanged, computed on the bounded Skill read worker pool:

```text
{
  "skills": [entry, ...],             // one per scanned package per source, never policy-filtered
  "archived": [archived, ...],        // every writable home's Skill Archive, per home newest first
  "agents": [agent_access, ...],      // Identity Agents in roster order
  "projects": [project_access, ...],  // by display_name (casefolded), then project_id
  "policy_diagnostics": [...],
  "stale_shared": [...]
}
```

- `entry`: `{id, editable_scope, source_kind, source_label, name, description, origin, owner_id, project_id, shared, shared_with, disabled, status, missing, optional_missing, warnings}`. `source_kind` names the scanned root (`_ManagerSource.kind`): `home` (`<data_dir>/skills`), `folder` (a `skill_directories` entry), `extension` (a loaded Extension's `skills/`), `bundled`, `project` or `agent`, so a client can say why a package without `editable_scope` cannot be edited; `source_label` is the folder or Extension name of a `folder`/`extension` root. `id` hashes (source root, package path, owner id), so it identifies one exact source package independently of names and is what `skill.inspect` takes. `owner_id` is set only for private homes, `project_id` only for Project Skill directories. `status` is `available`/`unavailable`/`invalid`, overridden by `disabled`.
- History fields of every entry (`SkillRuntime._record_fields`): `created_by` (the Skill History origin: `human`, `agent`, `reflection` or `librarian`), `created_at`, `changed_at`, `changed_by` (last change of package files, `null` without one) and `pinned`. They are filled only for a package with an `editable_scope` whose history record could be read; otherwise `null` and `pinned: false`. Reading them records a `baseline` for a package the history does not know yet (`skills/history.md`).
- Skill use (added by the `skill.inventory` RPC, not by `skill_inventory()`): `uses` (Sessions that activated the Skill) and `last_used_at` (latest activation, `null` when never) from `StatisticsService.skill_usage_async()`, keyed by Agent and bare name like the Statistics report, background Sessions excluded (`statistics.md`). A private entry counts its owner plus the receivers it is shared with that own no same-named package; any other entry counts every Agent that neither owns nor receives a private package of that name. When Statistics cannot answer, the RPC logs a WARNING and the entries carry neither field.
- `archived`: `{scope, archive_id, name, archived_at, reason, absorbed_into, archived_by, origin, description}` for the global home first, then each Identity Agent's home in roster order (`SkillAuthoringService.archived`, `skills/history.md`).
- `agent_access`: `{id, name, root_project_id, allowed_skills, excluded_skills, mode, skills: [{name, package_id, own, grant, available}]}`. `mode` is `all` when `allowed_skills` contains `*`, else `selected`. `skills` lists every Skill in the registry the Agent's own Runs resolve (`skills_for(root_project_id, id)` when the root Project exists, else `skills_for(None, id)`), sorted by name; policy-disabled Skills are absent there. `own` is true when the winning package lives in this Agent's private home, independent of `grant`.
- `project_access`: `{project_id, name, skills_project_disabled, skills_global_enabled, skills_bundled_enabled, skills: [{name, package_id, source, active}]}`, `skills` sorted by name. `source` is `project`/`global`/`bundled` from `project_skill_pool` (below); `active` means the name is in the Project's effective set (`effective_project_allowed_skills`). A Project removed while the inventory is assembled is omitted.
- `package_id` is the `id` of the inventory entry for the package that wins in that registry (first loadable entry with the same resolved package path), or `null` when none matches.

## Grants and availability

`grant` answers why an Agent may (not) use a Skill, first match wins:

1. `own` - the package lives in the Agent's private home and its name is not in `excluded_skills`.
2. `project` - the name is in the registry's `always_allowed` beyond the Agent's non-excluded own names, i.e. the root Project's effective set. It outranks an exclusion, also for a same-named own package (`own: true, grant: project`).
3. `excluded` - the name is in the Agent's `excluded_skills`; an excluded own package reports `own: true, grant: excluded`. A client toggles an own Skill the same way in `all` and `selected` mode: off adds the name to `excluded_skills`, on removes it.
4. `allowed` - `allowed_skills` is `*` or lists the name. Shared Skills land here or in the next case; they never get `own`.
5. `not_selected` - otherwise.

`available` is `registry.availability_for(name, allowed_skills).state == "available"`: the package's own requirements plus its Skill dependencies under this Agent's grants. It does not restate the grant, so a `not_selected` Skill may still report `available: true`, and an allowed Skill depending on an excluded or unselected one reports `false`.

## Project Skill pool

`SkillRuntime.project_skill_pool(project_id)` (Runtime delegate of the same name) classifies the cached Project bundle registry into `{"project", "global", "bundled"}`, each sorted by name: the Project's own Skills (`project`), the user's global home plus `skill_directories` plus loaded Extension Skills (`global`, origin tag `global`), and everything else shipped with vBot (`bundled`). A Project Skill shadows same-named global/bundled ones; packages turned off in the Skill Policy are absent. The Project scan preview (`scan` in the `project.add`, `project.show`, `project.set` and override responses, built by `server/rpc/project_methods.py::_scan_preview`) carries it as `skills: {project, bundled, global}` lists of `{name, description}` for the Project whitelist editor, and the inventory's `project_access` uses the same classification. Tests: `test_runtime_shared_skills.py` (`test_manager_projects_each_projects_skill_pool`, `test_manager_projects_each_agents_effective_skill_access`), `tests/server/rpc/test_project_methods.py`.

## Invalidation

The manager re-fetches on `resource_changed(kind="skills")`; publishers are listed in `server/events-and-reconnect.md` -> `resource_changed`. Agent changes that alter `agents` rows (`allowed_skills`, `excluded_skills`, `root_project_id`, create/rename/delete) publish only `agents`, and Project creation/removal publishes only `projects`/`agents`, so a client showing those rows must also re-fetch on those kinds.
