"""Skill scope resolution, inventory, sharing, and registry caches."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.agents import Agent, AgentStore
from core.extensions import ExtensionRegistry
from core.projects import Project, ProjectError, ProjectStore, effective_project_allowed_skills
from core.skills.policy import SkillPolicyService
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SKILL_ORIGIN_PROJECT_PREFIX,
    WILDCARD_ALLOWLIST,
    SkillMetadata,
    SkillRegistry,
    find_skill_package_dir,
    load_project_skill_registry,
    project_skill_origin,
    project_skills_dir,
    scan_project_skill_names,
    scan_skill_names,
)
from core.storage import StorageManager

_SKILLS_DIRNAME = "skills"
_AGENTS_DIRNAME = "agents"


def _scan_roots(
    storage: StorageManager,
    resources_path: Path,
    settings: dict[str, object],
    extensions: ExtensionRegistry | None,
    logger: Any,
) -> list[Path]:
    """Return the global Skill scan roots in first-found-wins precedence order.

    Every global source outranks the bundled Skills: the user's own global home
    (``<data_dir>/skills``) first, then the configured ``skill_directories`` in
    their listed order, then each loaded Extension's ``skills/`` folder, and the
    bundled ``resources/skills`` last. :func:`_origin_layers` relies on the bundled
    root being the final entry.
    """
    raw_directories = settings.get("skill_directories", [])
    extra_directories: list[Path] = []
    if not isinstance(raw_directories, list):
        logger.warning("settings.skill_directories must be a list; ignoring value")
    else:
        for raw_directory in raw_directories:
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                logger.warning("Ignoring invalid skill directory setting: %r", raw_directory)
                continue
            extra_directories.append(Path(raw_directory).expanduser())
    extension_directories = (
        [
            record.root_path / _SKILLS_DIRNAME
            for record in extensions.records()
            if record.status == "loaded"
        ]
        if extensions is not None
        else []
    )
    return [
        storage.data_dir / _SKILLS_DIRNAME,
        *extra_directories,
        *extension_directories,
        resources_path / _SKILLS_DIRNAME,
    ]


def _origin_layers(scan_roots: list[Path]) -> list[str | None]:
    """Return the origin tags parallel to :func:`_scan_roots` (bundled last)."""
    origins: list[str | None] = [SKILL_ORIGIN_GLOBAL for _ in scan_roots[:-1]]
    origins.append(SKILL_ORIGIN_BUNDLED)
    return origins


def load_global_skill_registry(
    *,
    storage: StorageManager,
    resources_path: Path,
    settings: dict[str, object],
    fallback_environment: dict[str, str],
    extensions: ExtensionRegistry | None,
    excluded_names: frozenset[str],
    logger: Any,
) -> SkillRegistry:
    environment = dict(fallback_environment)
    environment.update(os.environ)
    roots = _scan_roots(storage, resources_path, settings, extensions, logger)
    return SkillRegistry.load(
        roots[0],
        extra_dirs=roots[1:],
        environment=environment,
        origins=_origin_layers(roots),
        excluded_names=excluded_names,
    )


@dataclass(frozen=True)
class _ProjectSkillBundle:
    registry: SkillRegistry
    names: frozenset[str]


@dataclass(frozen=True)
class _ManagerSource:
    """One inventoried Skill root: its origin tag and owning Agent or Project."""

    root: Path
    origin: str | None
    owner_id: str | None
    project_id: str | None


class SkillRuntime:
    """Own the effective Skill layer and every scoped registry cache."""

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        policy: SkillPolicyService,
        storage: StorageManager,
        agents: AgentStore,
        projects: Callable[[], ProjectStore],
        extensions: ExtensionRegistry | None,
        resources_path: Path,
        logger: Any,
        reload_skills: Callable[[], None],
    ) -> None:
        self._skills = registry
        self._policy = policy
        self._storage = storage
        self._agents = agents
        self._get_projects = projects
        self._extensions = extensions
        self._resources_path = resources_path
        self._logger = logger
        self._reload = reload_skills
        self._project_skills: dict[str, _ProjectSkillBundle] = {}
        self._agent_skills: dict[tuple[str | None, str], SkillRegistry] = {}
        # Scans run in workers as well as on the Event Loop. Hold this lock only
        # around cache metadata; mutations must never wait for filesystem scans.
        self._cache_lock = threading.RLock()
        self._cache_generation = 0
        self._changed_callbacks: list[Callable[[], None]] = []

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to Skill package changes made outside the operator surface.

        Operator mutations publish their own invalidation; this channel reports
        changes an Agent makes through its Skill authoring Tool. Returns an
        unsubscribe function.
        """
        with self._cache_lock:
            self._changed_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._cache_lock:
                if callback in self._changed_callbacks:
                    self._changed_callbacks.remove(callback)

        return unsubscribe

    def notify_changed(self) -> None:
        """Tell subscribers that a Skill package was created, changed, or removed."""
        with self._cache_lock:
            callbacks = tuple(self._changed_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception as error:
                if self._logger is not None:
                    self._logger.error(
                        "Skill change callback failed: %s",
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )

    @property
    def registry(self) -> SkillRegistry:
        return self._skills

    @property
    def _projects(self) -> ProjectStore:
        return self._get_projects()

    def rebind(
        self,
        *,
        registry: SkillRegistry,
        extensions: ExtensionRegistry | None,
        logger: Any,
    ) -> None:
        self._skills = registry
        self._extensions = extensions
        self._logger = logger

    def replace_registry(self, registry: SkillRegistry) -> None:
        with self._cache_lock:
            self._skills = registry
            self.invalidate_project_skills()

    def reload_environment(self, fallback_environment: dict[str, str]) -> None:
        """Refresh requirements in every cached scope, including held Run references."""
        environment = self._skill_environment(fallback_environment)
        with self._cache_lock:
            self._cache_generation += 1
            self._skills.reload_environment(environment)
            for bundle in self._project_skills.values():
                bundle.registry.reload_environment(environment)
            for registry in self._agent_skills.values():
                registry.reload_environment(environment)

    def load_global_registry(self) -> SkillRegistry:
        return load_global_skill_registry(
            storage=self._storage,
            resources_path=self._resources_path,
            settings=self._storage.load_settings(),
            fallback_environment=self._storage.load_environment(),
            extensions=self._extensions,
            excluded_names=self._disabled_skill_names(),
            logger=self._logger,
        )

    def _disabled_skill_names(self) -> frozenset[str]:
        return self._policy.load().disabled

    def _skill_environment(self, fallback_environment: dict[str, str]) -> dict[str, str]:
        environment = dict(fallback_environment)
        environment.update(os.environ)
        return environment

    def _skill_scan_roots(self, settings: dict[str, object], resources_path: Path) -> list[Path]:
        return _scan_roots(self._storage, resources_path, settings, self._extensions, self._logger)

    def agent_skills_dir(self, agent_id: str) -> Path:
        """Return an agent's private skill home (``<data_dir>/agents/<id>/skills``)."""
        return self._storage.data_dir / _AGENTS_DIRNAME / agent_id / _SKILLS_DIRNAME

    def agent_owns_private_skill(self, agent_id: str, name: str) -> bool:
        """Whether an Identity Agent's private home currently loads that Skill name."""
        environment = self._skill_environment(self._storage.load_environment())
        return (
            find_skill_package_dir(self.agent_skills_dir(agent_id), name, environment) is not None
        )

    @property
    def global_skills_dir(self) -> Path:
        """Return the user-curated global skills directory (``<data_dir>/skills``)."""
        return self._storage.data_dir / _SKILLS_DIRNAME

    def skills_for(
        self, project_id: str | None, identity_agent_id: str | None = None
    ) -> SkillRegistry:
        """Return the skill registry a run should use, scoped to project and agent.

        ``project_id is None`` and ``identity_agent_id is None`` (a plain identity
        run) returns the global registry byte-for-byte. A set ``project_id`` returns
        the project's merged registry — the project's own skill directory (its
        declared source format's location) first,
        then the bundled pool. When ``identity_agent_id`` names an **identity** agent,
        its private home is layered on top when present (agent > project > global >
        bundled). The agent's own Skills and the effective Skill set of a selected
        Project are always allowed in that scoped registry: Project Context therefore
        grants what the Project uses without mutating the Agent's configured personal
        allowlist. An Identity Agent's ``excluded_skills`` are removed from what its
        ``allowed_skills`` grants in that scoped registry, never from those always
        allowed Skills; an Agent with exclusions always gets a scoped registry.
        This is the single seam every run-time skill consumer (prompt
        assembly, triggers, the ``skill`` tool, autocomplete) resolves through, so
        scoping lives in exactly one place.

        **Contract:** ``identity_agent_id`` carries the run's agent id only when the
        run executes as an identity agent (plain or rooted — a rooted run passes its
        home project as ``project_id``). A config-agent run passes ``None``: config
        agents own no private home, and agent ids are project-local, so a team slug
        that merely collides with an identity agent's id must never pull that
        identity agent's private skills into the project run (the project skill
        whitelist is a trust boundary; private skills bypass it as always-allowed).
        The identity-store existence check below is defense in depth against a stray
        ``agents/<id>/skills`` directory that belongs to no stored agent.
        """
        agent = self._agents.find(identity_agent_id) if identity_agent_id is not None else None
        if agent is not None:
            exclusions = frozenset(agent.excluded_skills)
            if (
                project_id is not None
                or exclusions
                or self.agent_skills_dir(agent.id).is_dir()
                or self._receives_shared_skills(agent.id)
            ):
                return self._agent_skill_registry(project_id, agent.id, exclusions)
        if project_id is None:
            return self._skills
        return self._project_skill_bundle(project_id).registry

    def refresh_skills_for(
        self, project_id: str | None, identity_agent_id: str | None = None
    ) -> SkillRegistry:
        """Rescan every Skill source, then resolve one fresh scoped registry.

        Compaction uses this as an explicit prompt-refresh boundary. The global reload
        also invalidates Project- and Agent-scoped caches, so the returned registry
        reflects bundled, global, extension, Project, and private Skill changes from
        one coherent scan generation.
        """
        self._reload()
        return self.skills_for(project_id, identity_agent_id)

    def project_own_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return a Project's own skills for explicit Project Context loading.

        Scans only the Project's own Skill directory (its declared Source Format's
        location), so the result is exactly the Project-owned Skills with their
        ``SKILL.md`` paths. The Project Tool lists them in its persisted result, and
        Chat routes later Skill activation through that loaded Project context. A
        missing directory yields an empty list.
        """
        project = self._projects.get(project_id)
        environment = self._skill_environment(self._storage.load_environment())
        registry = SkillRegistry.load(
            project_skills_dir(Path(project.cwd), project.source_format),
            environment=environment,
            excluded_names=self._disabled_skill_names(),
        )
        return registry.list_all()

    def project_context_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return the complete effective Skill set carried by Project Context.

        Project-owned Skills are active by default except explicit Project
        disables; bundled and global Skills join only through the Project's opt-in
        lists. This is the same Project policy used for Config Agents and the
        temporary Project grant applied to Identity Runs.
        """
        project = self._projects.get(project_id)
        bundle = self._project_skill_bundle(project_id)
        allowed_names = set(effective_project_allowed_skills(project, bundle.names))
        return [skill for skill in bundle.registry.list_all() if skill.name in allowed_names]

    def _manager_sources(
        self,
        projects: list[Project] | None = None,
        agents: list[Agent] | None = None,
    ) -> list[_ManagerSource]:
        roots = self._skill_scan_roots(self._storage.load_settings(), self._resources_path)
        sources = [
            _ManagerSource(root, origin, None, None)
            for root, origin in zip(roots, _origin_layers(roots), strict=True)
        ]
        sources.extend(
            _ManagerSource(
                project_skills_dir(Path(project.cwd), project.source_format),
                project_skill_origin(project.display_name),
                None,
                project.project_id,
            )
            for project in (self._projects.list() if projects is None else projects)
        )
        sources.extend(
            _ManagerSource(self.agent_skills_dir(agent.id), SKILL_ORIGIN_AGENT, agent.id, None)
            for agent in (self._agents.list() if agents is None else agents)
        )
        unique_sources: dict[tuple[Path, str | None], _ManagerSource] = {}
        for source in sources:
            unique_sources.setdefault((source.root.resolve(), source.owner_id), source)
        return list(unique_sources.values())

    @staticmethod
    def _manager_entry_id(root: Path, path: Path, owner_id: str | None) -> str:
        identity = f"{root.resolve().as_posix()}\0{path.resolve().as_posix()}\0{owner_id or ''}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def inspect_skill(self, entry_id: str) -> dict[str, Any]:
        """Read exactly one currently inventoried package without activating it."""
        environment = self._skill_environment(self._storage.load_environment())
        for source in self._manager_sources():
            registry = SkillRegistry.load(source.root, environment=environment)
            paths = [skill.path for skill in registry.list_all()]
            paths.extend(diagnostic.path for diagnostic in registry.invalid_diagnostics())
            for path in paths:
                if self._manager_entry_id(source.root, path, source.owner_id) == entry_id:
                    return {"id": entry_id, "content": path.read_text(encoding="utf-8")}
        raise ValueError("Skill is no longer present in the inventory")

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
        """
        environment = self._skill_environment(self._storage.load_environment())
        policy = self._policy.load()
        projects = self._projects.list()
        agents = self._agents.list()

        # (metadata, source, warnings, loadable) per scanned package.
        raw_entries: list[tuple[SkillMetadata, _ManagerSource, list[str], bool]] = []
        merged_roots: list[Path] = []
        merged_origins: list[str | None] = []

        def add_root(source: _ManagerSource) -> None:
            registry = SkillRegistry.load(source.root, environment=environment)
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
        )
        global_root = self.global_skills_dir.resolve()
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
            disabled = skill.name in policy.disabled
            if disabled:
                # The master switch outranks every other state in display.
                status = "disabled"
            owner_shared = policy.shared.get(owner_id, {}) if owner_id else {}
            shared_receivers = owner_shared.get(skill.name, frozenset())
            entry_id = self._manager_entry_id(root, skill.path, owner_id)
            if loadable:
                package_ids.setdefault(skill.path.resolve(), entry_id)
            skills.append(
                {
                    "id": entry_id,
                    "editable_scope": (
                        f"agent:{owner_id}"
                        if owner_id
                        else "global"
                        if root.resolve() == global_root
                        else None
                    )
                    if loadable
                    else None,
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
                }
            )
        project_pools = [
            self._project_skill_access(project, package_ids)
            for project in sorted(
                projects,
                key=lambda project: (project.display_name.casefold(), project.project_id),
            )
        ]
        return {
            "skills": skills,
            "agents": [self._agent_skill_access(agent, package_ids) for agent in agents],
            "projects": [pool for pool in project_pools if pool is not None],
            "policy_diagnostics": self._policy.validation_diagnostics(),
            "stale_shared": self._stale_shared_entries(policy),
        }

    def _agent_skill_access(self, agent: Agent, package_ids: dict[Path, str]) -> dict[str, Any]:
        """Project one Identity Agent's effective Skill grants for the manager.

        Uses the registry the Agent's own Runs resolve (its root Project when that
        Project still exists), so every listed name is one the Agent can see or be
        granted. ``grant`` names why a Skill is (not) granted: ``own`` private
        package, ``project`` granted by the root Project, ``excluded`` by
        ``excluded_skills``, ``allowed`` by ``allowed_skills``, else
        ``not_selected``. ``available`` reports whether its requirements, including
        Skill dependencies under this Agent's grants, are met.
        """
        root_project_id = agent.root_project_id
        project_id = (
            root_project_id
            if root_project_id is not None and self._projects.exists(root_project_id)
            else None
        )
        try:
            registry = self.skills_for(project_id, agent.id)
        except ProjectError:
            # The root Project vanished between the existence probe and the scan.
            registry = self.skills_for(None, agent.id)
        home = self.agent_skills_dir(agent.id).resolve()
        wildcard = WILDCARD_ALLOWLIST in agent.allowed_skills
        selected = set(agent.allowed_skills)
        excluded = set(agent.excluded_skills)
        skills: list[dict[str, Any]] = []
        for skill in registry.list_all():
            path = skill.path.resolve()
            if path.is_relative_to(home):
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
        self, project: Project, package_ids: dict[Path, str]
    ) -> dict[str, Any] | None:
        """Project one Project's Skill pool and which Skills it activates."""
        try:
            pool = self.project_skill_pool(project.project_id)
            names = self.project_skill_names(project.project_id)
        except ProjectError:
            # The Project was removed while the inventory was being assembled.
            return None
        active = set(effective_project_allowed_skills(project, names))
        entries = [
            {
                "name": skill.name,
                "package_id": package_ids.get(skill.path.resolve()),
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

    def _stale_shared_entries(self, policy: Any) -> list[dict[str, Any]]:
        """Report shared policy entries whose owner or package no longer exists."""
        environment = self._skill_environment(self._storage.load_environment())
        stale: list[dict[str, Any]] = []
        for owner_id, skills in sorted(policy.shared.items()):
            owner_exists = self._agents.exists(owner_id)
            for name in sorted(skills):
                if not owner_exists or (
                    find_skill_package_dir(self.agent_skills_dir(owner_id), name, environment)
                    is None
                ):
                    stale.append({"agent_id": owner_id, "name": name})
        return stale

    def project_skill_pool(self, project_id: str) -> dict[str, list[SkillMetadata]]:
        """Return the Skills a Project can activate, grouped by source.

        ``project`` holds the Project's own Skills, active unless the Project
        disables them. ``global`` holds the user's global home, configured
        ``skill_directories`` and loaded Extension Skills; ``bundled`` holds the
        Skills shipped with vBot. Global and bundled Skills become active only
        when the Project opts them in. A Project Skill shadows same-named global
        and bundled ones, and Skills disabled by the Skill Policy are absent. Each
        group is sorted by name.
        """
        bundle = self._project_skill_bundle(project_id)
        pool: dict[str, list[SkillMetadata]] = {"project": [], "global": [], "bundled": []}
        for skill in bundle.registry.list_all():
            if skill.name in bundle.names:
                pool["project"].append(skill)
            elif skill.origin == SKILL_ORIGIN_GLOBAL:
                pool["global"].append(skill)
            else:
                pool["bundled"].append(skill)
        return pool

    def project_skill_names(self, project_id: str | None) -> frozenset[str]:
        """Return the names of a project's own scanned skills (empty for identity).

        The resolver uses this to compute a config agent's effective skills
        ``(project skills − disabled) ∪ enabled-bundled``. Cached with the project's
        merged registry so it does not re-scan the repo every resolve.
        """
        if project_id is None:
            return frozenset()
        return self._project_skill_bundle(project_id).names

    def invalidate_project_skills(self, project_id: str | None = None) -> None:
        """Drop the cached project skills for one project, or for all when ``None``.

        Agent-aware registries embed the project layer, so this also drops the
        cached agent registries for that project (or all of them when ``None``) to
        keep them coherent with the project pool.
        """
        with self._cache_lock:
            self._cache_generation += 1
            if project_id is None:
                self._project_skills.clear()
                self._agent_skills.clear()
                return
            self._project_skills.pop(project_id, None)
            self._drop_agent_skills(lambda key: key[0] == project_id)

    def invalidate_agent_skills(self, agent_id: str | None = None) -> None:
        """Drop a Skill owner's and its receivers' caches, or all when ``None``.

        Private-home writes also change the shared layer of every receiver. Keep
        that dependency here so Tool and Accessor writers only identify the owner,
        including when creating or deleting a package named by an existing share.
        All Project contexts of each affected Identity Agent must rebuild.
        """
        affected = {agent_id}
        if agent_id is not None:
            for receivers in self._policy.load().shared.get(agent_id, {}).values():
                affected.update(receivers)
        with self._cache_lock:
            self._cache_generation += 1
            if agent_id is None:
                self._agent_skills.clear()
                return
            self._drop_agent_skills(lambda key: key[1] in affected)

    def _drop_agent_skills(self, predicate: Callable[[tuple[str | None, str]], bool]) -> None:
        for key in [key for key in self._agent_skills if predicate(key)]:
            del self._agent_skills[key]

    def _agent_skill_registry(
        self, project_id: str | None, agent_id: str, exclusions: frozenset[str]
    ) -> SkillRegistry:
        # A cached registry is reused only while it carries the Agent's current
        # ``excluded_skills``: an Agent update (or a hand edit of agent.json) that
        # changes them replaces the entry without any explicit invalidation.
        key = (project_id, agent_id)
        while True:
            with self._cache_lock:
                cached = self._agent_skills.get(key)
                if cached is not None and cached.allowlist_exclusions == exclusions:
                    return cached
                generation = self._cache_generation
            registry = self._build_agent_skill_registry(project_id, agent_id, exclusions)
            with self._cache_lock:
                if generation == self._cache_generation:
                    current = self._agent_skills.get(key)
                    if current is not None and current.allowlist_exclusions == exclusions:
                        return current
                    self._agent_skills[key] = registry
                    return registry

    def _build_agent_skill_registry(
        self,
        project_id: str | None,
        agent_id: str,
        exclusions: frozenset[str] = frozenset(),
    ) -> SkillRegistry:
        settings = self._storage.load_settings()
        environment = self._skill_environment(self._storage.load_environment())
        agent_root = self.agent_skills_dir(agent_id)
        scan_roots = self._skill_scan_roots(settings, self._resources_path)
        roots: list[Path] = [agent_root]
        origins: list[str | None] = [SKILL_ORIGIN_AGENT]
        project_allowed_names: set[str] = set()
        if project_id is not None:
            project = self._projects.get(project_id)
            roots.append(project_skills_dir(Path(project.cwd), project.source_format))
            origins.append(project_skill_origin(project.display_name))
            project_allowed_names.update(
                effective_project_allowed_skills(
                    project,
                    self._project_skill_bundle(project_id).names,
                )
            )
        # Shared layer (own > project > shared > global > bundled): every other
        # owner's individually resolved shared package directories — never an
        # owner's whole skills home, so unshared neighbours cannot leak. They are
        # tagged with the receiver-facing Agent origin, so catalogs render them
        # indistinguishably among "Your own skills", and they are NOT added to
        # ``always_allowed``: they pass through the receiver's ``allowed_skills``
        # filter exactly like global skills.
        shared_package_dirs = self._shared_package_dirs(agent_id)
        roots.extend(shared_package_dirs)
        origins.extend(SKILL_ORIGIN_AGENT for _ in shared_package_dirs)
        roots.extend(scan_roots)
        origins.extend(_origin_layers(scan_roots))
        # First-found-wins ordering makes agent skills win over project, project over
        # shared, shared over bundled. The agent's own skills are always-allowed for
        # it, so they bypass the owner's ``allowed_skills`` filter without leaking to
        # other agents (whose registries never scan this home). Project Context is
        # itself the authorization to use that Project's effective Skill set: those
        # exact Project-granted names also bypass the Identity Agent's unrelated
        # personal allowlist while this project-scoped registry is active. The
        # Agent's ``excluded_skills`` narrow only its allowlist grant, never these.
        agent_own_names = scan_skill_names(agent_root, environment)
        return SkillRegistry.load(
            roots[0],
            extra_dirs=roots[1:],
            environment=environment,
            always_allowed=agent_own_names | project_allowed_names,
            origins=origins,
            excluded_names=self._disabled_skill_names(),
            allowlist_exclusions=exclusions,
        )

    def _receives_shared_skills(self, receiver_agent_id: str) -> bool:
        """Whether any other Identity Agent has shared Skills to this receiver.

        Part of the ``skills_for`` scoping decision: an agent with no private home
        and no Project must still get a scoped registry when others share to it.
        """
        shared = self._policy.load().shared
        for owner_id, skills in shared.items():
            if owner_id == receiver_agent_id:
                continue
            for receivers in skills.values():
                if receiver_agent_id in receivers:
                    return True
        return False

    def _resolve_shared_skills_dir(self, receiver_agent_id: str, name: str) -> Path | None:
        """Return the owning skills home of the effective shared Skill instance.

        Mirrors the registry's first-found ordering (sorted owner ids), so a
        ``skill_manage`` mutation lands in exactly the package activation serves.
        ``None`` when no other agent shares that name with the receiver.
        """
        shared = self._policy.load().shared
        if not shared:
            return None
        environment = self._skill_environment(self._storage.load_environment())
        for owner_id, skills in sorted(shared.items()):
            if owner_id == receiver_agent_id:
                continue
            receivers = skills.get(name)
            if receivers is None or receiver_agent_id not in receivers:
                continue
            if not self._agents.exists(owner_id):
                continue
            package_dir = find_skill_package_dir(self.agent_skills_dir(owner_id), name, environment)
            if package_dir is not None:
                return package_dir.parent
        return None

    def _resolve_external_skill_scope(
        self, agent_id: str, name: str, project_id: str | None
    ) -> str | None:
        """Return where ``name`` resolves outside the caller's own private home.

        The agent-scoped registry is the same seam ``skill`` resolves through, so
        this answers how the name is *visible* to the agent (bundled / global /
        project / shared) — never how the authoring core sees it, which only knows
        the target root. ``agent`` origin with the name absent from own home means a
        Skill shared into this agent; ``None`` means genuinely unknown.
        """
        registry = self.skills_for(project_id, agent_id)
        try:
            origin = registry.get(name).origin
        except KeyError:
            return None
        if origin == SKILL_ORIGIN_AGENT:
            return "shared"
        if origin == SKILL_ORIGIN_BUNDLED:
            return "bundled"
        if origin == SKILL_ORIGIN_GLOBAL:
            return "global"
        if origin is not None and origin.startswith(SKILL_ORIGIN_PROJECT_PREFIX):
            return "project"
        return None

    def _shared_package_dirs(self, receiver_agent_id: str) -> list[Path]:
        """Resolve every owner's shared private Skill packages for one receiver.

        Deterministic order — sorted by owner id, then skill name — so first-found
        collision handling matches activation exactly. Only existing Identity
        Agents contribute; stale entries (an unknown owner id or a vanished package
        directory) are ignored at load with a warning and stay in the policy file
        for the human manager to clean up. Only skills whose receiver list
        includes this receiver are inserted.
        """
        shared = self._policy.load().shared
        if not shared:
            return []
        environment = self._skill_environment(self._storage.load_environment())
        directories: list[Path] = []
        for owner_id, skills in sorted(shared.items()):
            if owner_id == receiver_agent_id:
                # The owner keeps its own copy via its private-home layer.
                continue
            if not self._agents.exists(owner_id):
                if self._logger is not None:
                    self._logger.warning(
                        "Ignoring stale shared skills for unknown agent '%s'",
                        owner_id,
                    )
                continue
            owner_root = self.agent_skills_dir(owner_id)
            for name, receivers in sorted(skills.items()):
                if receiver_agent_id not in receivers:
                    continue
                package_dir = find_skill_package_dir(owner_root, name, environment)
                if package_dir is None:
                    if self._logger is not None:
                        self._logger.warning(
                            "Ignoring stale shared skill '%s' of agent '%s' "
                            "(no such private skill)",
                            name,
                            owner_id,
                        )
                    continue
                directories.append(package_dir)
        return directories

    def _project_skill_bundle(self, project_id: str) -> _ProjectSkillBundle:
        while True:
            with self._cache_lock:
                cached = self._project_skills.get(project_id)
                if cached is not None:
                    return cached
                generation = self._cache_generation
            bundle = self._build_project_skill_bundle(project_id)
            with self._cache_lock:
                if generation == self._cache_generation:
                    return self._project_skills.setdefault(project_id, bundle)

    def _build_project_skill_bundle(self, project_id: str) -> _ProjectSkillBundle:
        project = self._projects.get(project_id)
        project_cwd = Path(project.cwd)
        settings = self._storage.load_settings()
        scan_roots = self._skill_scan_roots(settings, self._resources_path)
        environment = self._skill_environment(self._storage.load_environment())
        disabled = self._disabled_skill_names()
        registry = load_project_skill_registry(
            project_cwd,
            project.source_format,
            scan_roots,
            environment,
            project_origin=project_skill_origin(project.display_name),
            bundled_origins=_origin_layers(scan_roots),
            excluded_names=disabled,
        )
        # The resolver's config-agent input must be clean of disabled names too —
        # a disabled project skill is invisible everywhere, including opt-ins.
        names = scan_project_skill_names(project_cwd, project.source_format, environment)
        return _ProjectSkillBundle(registry=registry, names=names - disabled)
