# Skill Manager Inventory

Task-gated reference for the human Skill manager's read model: `skill.inventory` (`SkillRuntime.skill_inventory()`), its per-Agent and per-Project access projections, the Project Skill pool behind `project.show`, and the events that invalidate them. Read it when changing those payloads or a client that consumes them. Semantics of Agent Skill Exclusions, Disabled and Shared Skills live in `skills.md` -> Terms.

## Inventory shape

`skill.inventory` returns `SkillRuntime.skill_inventory()` unchanged, computed on the bounded Skill read worker pool:

```text
{
  "skills": [entry, ...],             // one per scanned package per source, never policy-filtered
  "agents": [agent_access, ...],      // Identity Agents in roster order
  "projects": [project_access, ...],  // by display_name (casefolded), then project_id
  "policy_diagnostics": [...],
  "stale_shared": [...]
}
```

- `entry`: `{id, editable_scope, source_label, name, description, origin, owner_id, project_id, shared, shared_with, disabled, status, missing, optional_missing, warnings}`. `id` hashes (source root, package path, owner id), so it identifies one exact source package independently of names and is what `skill.inspect` takes. `owner_id` is set only for private homes, `project_id` only for Project Skill directories. `status` is `available`/`unavailable`/`invalid`, overridden by `disabled`.
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

`SkillRuntime.project_skill_pool(project_id)` (Runtime delegate of the same name) classifies the cached Project bundle registry into `{"project", "global", "bundled"}`, each sorted by name: the Project's own Skills (`project`), the user's global home plus `skill_directories` plus loaded Extension Skills (`global`, origin tag `global`), and everything else shipped with vBot (`bundled`). A Project Skill shadows same-named global/bundled ones; policy-disabled Skills are absent. The Project scan preview (`scan` in the `project.add`, `project.show`, `project.set` and override responses, built by `server/rpc/project_methods.py::_scan_preview`) carries it as `skills: {project, bundled, global}` lists of `{name, description}` for the Project whitelist editor, and the inventory's `project_access` uses the same classification. Tests: `test_runtime_shared_skills.py` (`test_manager_projects_each_projects_skill_pool`, `test_manager_projects_each_agents_effective_skill_access`), `tests/server/rpc/test_project_methods.py`.

## Invalidation

The manager re-fetches on `resource_changed(kind="skills")`; publishers are listed in `server/events-and-reconnect.md` -> `resource_changed`. Agent changes that alter `agents` rows (`allowed_skills`, `excluded_skills`, `root_project_id`, create/rename/delete) publish only `agents`, and Project creation/removal publishes only `projects`/`agents`, so a client showing those rows must also re-fetch on those kinds.
