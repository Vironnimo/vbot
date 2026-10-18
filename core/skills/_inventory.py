"""Human Skill inventory, package selection and access projections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import Agent
from core.projects import Project, effective_project_allowed_skills
from core.skills._global_scanning import _origin_layers
from core.skills.authoring import SkillRecord
from core.skills.policy import SkillPackageRef
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_GLOBAL,
    WILDCARD_ALLOWLIST,
    SkillMetadata,
    SkillRegistry,
    SkillScan,
    project_skill_origin,
    scan_skill_names,
)

if TYPE_CHECKING:
    from core.skills.runtime import SkillRuntime, _ProjectSkillBundle


class SkillInventory:
    def __init__(self, owner: SkillRuntime) -> None:
        self._owner = owner

    def _manager_sources(
        self,
        projects: list[Project] | None = None,
        agents: list[Agent] | None = None,
    ) -> list[_ManagerSource]:
        global_roots = self._owner._global_roots(self._owner._storage.load_settings())
        origins = _origin_layers([root.root for root in global_roots])
        sources = [
            _ManagerSource(root.root, origin, None, None, root.kind, root.location)
            for root, origin in zip(global_roots, origins, strict=True)
        ]
        sources.extend(
            _ManagerSource(
                root,
                project_skill_origin(project.display_name),
                None,
                project.project_id,
                "project",
            )
            for project in (
                [
                    self._owner._projects.refresh_sources(project.project_id)
                    for project in self._owner._projects.list()
                ]
                if projects is None
                else projects
            )
            for root in project.skill_roots
        )
        sources.extend(
            _ManagerSource(
                self._owner.agent_skills_dir(agent.id), SKILL_ORIGIN_AGENT, agent.id, None, "agent"
            )
            for agent in (self._owner._agents.list() if agents is None else agents)
        )
        unique_sources: dict[tuple[Path, str | None], _ManagerSource] = {}
        for source in sources:
            unique_sources.setdefault((source.root.resolve(), source.owner_id), source)
        return list(unique_sources.values())

    @staticmethod
    def _manager_entry_id(root: Path, path: Path, owner_id: str | None) -> str:
        identity = f"{root.resolve().as_posix()}\0{path.resolve().as_posix()}\0{owner_id or ''}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _inventoried_package(
        self, entry_id: str, *, loadable_only: bool = False
    ) -> tuple[_ManagerSource, Path, str]:
        """Find one inventoried package: its source, ``SKILL.md`` path and name.

        Raises ``ValueError`` when no current package has that inventory id.
        """
        environment = self._owner._skill_environment(self._owner._storage.load_environment())
        for source in self._manager_sources():
            registry = SkillRegistry.load(source.root, environment=environment)
            packages = [(skill.path, skill.name) for skill in registry.list_all()]
            if not loadable_only:
                packages.extend(
                    (diagnostic.path, diagnostic.name)
                    for diagnostic in registry.invalid_diagnostics()
                )
            for path, name in packages:
                if self._manager_entry_id(source.root, path, source.owner_id) == entry_id:
                    return source, path, name
        raise ValueError("Skill is no longer present in the inventory")

    def inspect_skill(self, entry_id: str) -> dict[str, Any]:
        """Read exactly one currently inventoried package without activating it."""
        _, path, _ = self._inventoried_package(entry_id)
        return {"id": entry_id, "content": path.read_text(encoding="utf-8")}

    def set_package_disabled(self, entry_id: str, *, disabled: bool) -> dict[str, Any]:
        """Turn the inventoried package ``entry_id`` off, or on again.

        Only that package changes: a same-named package of another source stays
        as it is, and one that this package hid from the same pool loads again.
        A Project's own Skill is turned off in its Project
        (``skills_project_disabled``); every other package in the Skill Policy.
        Returns ``name``, ``disabled`` and the ``project_id`` of a Project Skill
        (``None`` otherwise). The caller reloads the global registry for a policy
        change. Raises ``ValueError`` for an unknown or unloadable package, and
        ``SkillPolicyError`` or ``ProjectError`` when the change cannot be saved.
        """
        source, _, name = self._inventoried_package(entry_id, loadable_only=True)
        ref = source.package_ref(name)
        if ref is None and source.project_id is not None:
            project = self._owner._projects.get(source.project_id)
            names = [entry for entry in project.skills_project_disabled if entry != name]
            if disabled:
                names.append(name)
            self._owner._projects.update(project.project_id, skills_project_disabled=names)
            self._owner.invalidate_project_skills(project.project_id)
        elif ref is not None:
            self._owner._policy.set_package_disabled(ref, disabled=disabled)
        return {"name": name, "disabled": disabled, "project_id": source.project_id}

    def skill_inventory(self) -> dict[str, Any]:
        """One pass over every Skill source for the human manager (no exclusions).

        Unlike ``skills_for`` this never applies the policy disable switch, so a
        disabled Skill stays visible and manageable here. Every scanned package
        is listed per source — a same-name Skill in two sources appears once per
        origin — annotated with its origin, owner (private homes only), Project
        (Project Skill directories only), share and disable state, availability,
        and warnings. Availability evaluates each package's own requirements
        against one merged dependency registry, even when another package with the
        same name takes precedence there. Stale policy entries (an unknown owner or
        a vanished package) are reported for cleanup, not silently dropped.

        ``agents`` projects each Identity Agent's effective Skill access (roster
        order) and ``projects`` each Project's Skill pool (by display name). Their
        ``package_id`` names the inventory entry of the package that wins there.

        A package in a writable home (``editable_scope`` set) carries its history
        record: ``created_by`` (``human``, ``agent``, ``reflection`` or
        ``librarian``), ``created_at``, ``changed_at``, ``changed_by`` and
        ``pinned``. ``archived`` lists every writable home's archived packages.
        """
        environment = self._owner._skill_environment(self._owner._storage.load_environment())
        policy = self._owner._policy.load()
        scan = SkillScan()
        projects = [
            self._owner._projects.refresh_sources(project.project_id, skill_scan=scan)
            for project in self._owner._projects.list()
        ]
        project_disabled = {
            project.project_id: frozenset(project.skills_project_disabled) for project in projects
        }
        agents = self._owner._agents.list()

        # (metadata, source, warnings, loadable) per scanned package.
        raw_entries: list[tuple[SkillMetadata, _ManagerSource, list[str], bool]] = []
        merged_roots: list[Path] = []
        merged_origins: list[str | None] = []

        def add_root(source: _ManagerSource) -> None:
            registry = SkillRegistry.load(source.root, environment=environment, scan=scan)
            for skill in registry.list_all():
                raw_entries.append((skill, source, registry.warnings_for(skill.name), True))
            for diagnostic in registry.invalid_diagnostics():
                placeholder = SkillMetadata(
                    name=diagnostic.name, description="", path=diagnostic.path
                )
                raw_entries.append((placeholder, source, diagnostic.warnings, False))
            merged_roots.append(source.root)
            merged_origins.append(source.origin)

        for source in self._manager_sources(projects, agents):
            add_root(source)

        merged = SkillRegistry.load(
            merged_roots[0],
            extra_dirs=merged_roots[1:],
            environment=environment,
            origins=merged_origins,
            scan=scan,
        )
        global_root = self._owner.global_skills_dir.resolve()
        records = self._home_records(agents)
        skills: list[dict[str, Any]] = []
        package_ids: dict[Path, str] = {}
        for skill, source, warnings, loadable in raw_entries:
            root, origin, owner_id = source.root, source.origin, source.owner_id
            if loadable:
                availability = merged.availability_for_package(skill)
                missing = list(availability.missing)
                optional_missing = list(availability.optional_missing)
                status = "available" if availability.state == "available" else "unavailable"
            else:
                missing = []
                optional_missing = []
                status = "invalid"
            ref = source.package_ref(skill.name)
            disabled = (
                ref in policy.disabled_packages
                if ref is not None
                else skill.name in project_disabled.get(source.project_id or "", frozenset())
            )
            if disabled:
                # A package turned off outranks every other state in display.
                status = "disabled"
            owner_shared = policy.shared.get(owner_id, {}) if owner_id else {}
            shared_receivers = owner_shared.get(skill.name, frozenset())
            entry_id = self._manager_entry_id(root, skill.path, owner_id)
            if loadable:
                package_ids.setdefault(skill.path, entry_id)
            editable_scope = (
                (
                    f"agent:{owner_id}"
                    if owner_id
                    else "global"
                    if root.resolve() == global_root
                    else None
                )
                if loadable
                else None
            )
            record = (
                records.get(editable_scope, {}).get(skill.path.parent.name)
                if editable_scope
                else None
            )
            # Policy entries use the visible name; usage must distinguish the
            # concrete loaded directory even when frontmatter names collide.
            package = source.package_ref(skill.path.parent.name) or SkillPackageRef(
                "folder", skill.path.parent.name, source.root.resolve().as_posix()
            )
            skills.append(
                {
                    "id": entry_id,
                    "package": package.to_dict(),
                    "editable_scope": editable_scope,
                    "source_kind": source.kind,
                    "source_label": (root.parent.name if root.name == "skills" else root.name)
                    if origin == SKILL_ORIGIN_GLOBAL and root.resolve() != global_root
                    else None,
                    "name": skill.name,
                    "description": skill.description,
                    "origin": origin,
                    "owner_id": owner_id,
                    "project_id": source.project_id,
                    "shared": bool(shared_receivers),
                    "shared_with": sorted(shared_receivers),
                    "disabled": disabled,
                    "status": status,
                    "missing": missing,
                    "optional_missing": optional_missing,
                    "warnings": warnings,
                    **_record_fields(record),
                }
            )
        projects_by_id = {project.project_id: project for project in projects}
        bundles = {
            project.project_id: self._owner._build_project_skill_bundle(
                project.project_id, project=project, scan=scan
            )
            for project in projects
        }
        project_pools = [
            self._project_skill_access(project, package_ids, bundles[project.project_id])
            for project in sorted(
                projects,
                key=lambda project: (project.display_name.casefold(), project.project_id),
            )
        ]
        return {
            "skills": skills,
            "archived": self._archived_entries(agents),
            "agents": [
                self._agent_skill_access(agent, package_ids, projects_by_id, bundles, scan)
                for agent in agents
            ],
            "projects": project_pools,
            "policy_diagnostics": self._owner._policy.validation_diagnostics(),
            "stale_shared": self._stale_shared_entries(policy, scan=scan),
        }

    def _writable_homes(self, agents: list[Agent]) -> list[tuple[str, Path]]:
        homes = [("global", self._owner.global_skills_dir)]
        homes.extend(
            (f"agent:{agent.id}", self._owner.agent_skills_dir(agent.id)) for agent in agents
        )
        return homes

    def _home_records(self, agents: list[Agent]) -> dict[str, dict[str, SkillRecord]]:
        if self._owner._authoring is None:
            return {}
        return {
            scope: self._owner._authoring.records(home)
            for scope, home in self._writable_homes(agents)
            if home.is_dir()
        }

    def _archived_entries(self, agents: list[Agent]) -> list[dict[str, Any]]:
        if self._owner._authoring is None:
            return []
        entries: list[dict[str, Any]] = []
        for scope, home in self._writable_homes(agents):
            entries.extend(
                {"scope": scope, **archived.to_dict()}
                for archived in self._owner._authoring.archived(home)
            )
        return entries

    def _agent_skill_access(
        self,
        agent: Agent,
        package_ids: dict[Path, str],
        projects: dict[str, Project],
        bundles: dict[str, _ProjectSkillBundle],
        scan: SkillScan,
    ) -> dict[str, Any]:
        """Project one Identity Agent's effective Skill grants for the manager.

        Uses the registry a new Session of the Agent resolves (its default Project
        when that Project still exists; an existing Session keeps the Project it
        works in), so every listed name is one the Agent can see or be granted.
        ``own`` marks a package from this Agent's private home. ``grant`` names why
        a Skill is (not) granted, first match wins: ``own`` private package not in
        ``excluded_skills``, ``project`` granted by the default Project (which
        outranks an exclusion), ``excluded`` by ``excluded_skills``, ``allowed`` by
        ``allowed_skills``, else ``not_selected``. ``available`` reports whether its
        requirements, including Skill dependencies under this Agent's grants, are met.
        """
        root_project_id = agent.root_project_id
        project = projects.get(root_project_id) if root_project_id is not None else None
        project_id = project.project_id if project is not None else None
        registry = self._owner._build_agent_skill_registry(
            project_id,
            agent.id,
            frozenset(agent.excluded_skills),
            project=project,
            project_bundle=bundles.get(project_id) if project_id is not None else None,
            scan=scan,
        )
        home = self._owner.agent_skills_dir(agent.id).resolve()
        wildcard = WILDCARD_ALLOWLIST in agent.allowed_skills
        selected = set(agent.allowed_skills)
        excluded = set(agent.excluded_skills)
        skills: list[dict[str, Any]] = []
        for skill in registry.list_all():
            path = skill.path
            own = path.is_relative_to(home)
            if own and skill.name not in excluded:
                grant = "own"
            elif skill.name in registry.always_allowed:
                grant = "project"
            elif skill.name in excluded:
                grant = "excluded"
            elif wildcard or skill.name in selected:
                grant = "allowed"
            else:
                grant = "not_selected"
            availability = registry.availability_for(skill.name, agent.allowed_skills)
            skills.append(
                {
                    "name": skill.name,
                    "package_id": package_ids.get(path),
                    "own": own,
                    "grant": grant,
                    "available": availability.state == "available",
                }
            )
        return {
            "id": agent.id,
            "name": agent.name,
            "root_project_id": root_project_id,
            "allowed_skills": list(agent.allowed_skills),
            "excluded_skills": list(agent.excluded_skills),
            "mode": "all" if wildcard else "selected",
            "skills": skills,
        }

    def _project_skill_access(
        self, project: Project, package_ids: dict[Path, str], bundle: _ProjectSkillBundle
    ) -> dict[str, Any]:
        """Project one Project's Skill pool and which Skills it activates."""
        pool = self._owner._classify_project_skills(bundle)
        active = set(effective_project_allowed_skills(project, bundle.names))
        entries = [
            {
                "name": skill.name,
                "package_id": package_ids.get(skill.path),
                "source": source,
                "active": skill.name in active,
            }
            for source, skills in pool.items()
            for skill in skills
        ]
        return {
            "project_id": project.project_id,
            "name": project.display_name,
            "skills_project_disabled": list(project.skills_project_disabled),
            "skills_global_enabled": list(project.skills_global_enabled),
            "skills_bundled_enabled": list(project.skills_bundled_enabled),
            "skills": sorted(entries, key=lambda entry: str(entry["name"])),
        }

    def _stale_shared_entries(
        self, policy: Any, *, scan: SkillScan | None = None
    ) -> list[dict[str, Any]]:
        """Report shared policy entries whose owner or package no longer exists."""
        environment = self._owner._skill_environment(self._owner._storage.load_environment())
        stale: list[dict[str, Any]] = []
        for owner_id, skills in sorted(policy.shared.items()):
            names = (
                scan_skill_names(self._owner.agent_skills_dir(owner_id), environment, scan=scan)
                if skills and self._owner._agents.exists(owner_id)
                else frozenset()
            )
            for name in sorted(skills):
                if name not in names:
                    stale.append({"agent_id": owner_id, "name": name})
        return stale


@dataclass(frozen=True)
class _ManagerSource:
    """One inventoried Skill root: its origin tag, owning Agent or Project, and kind.

    ``kind`` says where the root comes from, so a client can explain why a
    package is or is not editable: ``home`` (the user's global home), ``folder``
    (a configured ``skill_directories`` entry), ``extension`` (a loaded
    Extension's ``skills/``), ``bundled``, ``project`` or ``agent``.
    ``location`` names the folder (as configured) or the Extension.
    """

    root: Path
    origin: str | None
    owner_id: str | None
    project_id: str | None
    kind: str
    location: str | None = None

    def package_ref(self, name: str) -> SkillPackageRef | None:
        """The Skill Policy entry that turns this root's package ``name`` off.

        ``None`` for a Project's own Skill, which its Project turns off.
        """
        if self.kind == "project":
            return None
        if self.kind == "agent":
            return SkillPackageRef("agent", name, self.owner_id)
        return SkillPackageRef(self.kind, name, self.location)


def _record_fields(record: SkillRecord | None) -> dict[str, Any]:
    """Inventory fields of a writable package's history record."""
    if record is None:
        return {
            "created_by": None,
            "created_at": None,
            "changed_at": None,
            "changed_by": None,
            "pinned": False,
        }
    return {
        "created_by": record.origin,
        "created_at": record.created_at,
        "changed_at": record.changed_at,
        "changed_by": record.changed_by,
        "pinned": record.pinned,
    }
