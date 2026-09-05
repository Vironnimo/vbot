"""Skill scope resolution, inventory, sharing, and registry caches."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.agents import AgentStore
from core.extensions import ExtensionRegistry
from core.projects import ProjectStore, effective_project_allowed_skills
from core.skills.policy import SkillPolicyService
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SKILL_ORIGIN_PROJECT_PREFIX,
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
        resources_path / _SKILLS_DIRNAME,
        *extra_directories,
        *extension_directories,
    ]


def _origin_layers(scan_roots: list[Path]) -> list[str | None]:
    origins: list[str | None] = [SKILL_ORIGIN_GLOBAL, SKILL_ORIGIN_BUNDLED]
    origins.extend(SKILL_ORIGIN_GLOBAL for _ in scan_roots[2:])
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
        self._skills = registry
        self.invalidate_project_skills()

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

    def _ensure_started(self) -> None:
        return

    def _extra_skill_directories(self, settings: dict[str, object]) -> list[Path]:
        return _scan_roots(
            self._storage,
            self._resources_path,
            settings,
            None,
            self._logger,
        )[2:]

    def _disabled_skill_names(self) -> frozenset[str]:
        return self._policy.load().disabled

    def _skill_environment(self, fallback_environment: dict[str, str]) -> dict[str, str]:
        environment = dict(fallback_environment)
        environment.update(os.environ)
        return environment

    def _skill_scan_roots(self, settings: dict[str, object], resources_path: Path) -> list[Path]:
        """Return the ordered bundled skill scan roots, data dir first.

        One source of the bundled skill roots so the global registry and every
        project-scoped registry scan exactly the same directories
        (``<data_dir>/skills``, the bundled ``resources/skills``, the
        settings-configured extras, then the ``skills/`` folder of every loaded
        extension). A project registry prepends its own skill directory (its
        declared source format's location) ahead of these. Everything from the
        bundled root onward is tagged ``global`` by ``_bundled_skill_origins``, so
        extension-bundled skills present as global skills; the user's own
        ``<data_dir>/skills`` is scanned first and therefore wins a name collision.
        """
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return [
            self._storage.data_dir / _SKILLS_DIRNAME,
            resources_path / _SKILLS_DIRNAME,
            *self._extra_skill_directories(settings),
            *self._extension_skill_dirs(),
        ]

    def _extension_skill_dirs(self) -> list[Path]:
        """Return the ``skills/`` directory of every currently-loaded extension.

        A loaded extension in package/directory form may bundle skills under
        ``<extension>/skills/`` (GLOSSARY -> Skill); ``_skill_scan_roots`` folds
        them into the global pool, so an extension ships a skill with no code —
        only the folder. Only ``loaded`` records contribute: a disabled, failed, or
        overridden extension adds nothing. A single-file extension's ``root_path``
        is its ``.py`` file, whose ``skills`` child is not a directory and is simply
        skipped by the scan. Empty until the extension layer exists.
        """
        if self._extensions is None:
            return []
        return [
            record.root_path / _SKILLS_DIRNAME
            for record in self._extensions.records()
            if record.status == "loaded"
        ]

    @staticmethod
    def _bundled_skill_origins(scan_roots: list[Path]) -> list[str | None]:
        """Origin tags parallel to ``_skill_scan_roots``: data-dir global, then bundled.

        The first root is the data-dir global pool, the second the shipped bundled
        pool; any configured extra ``skill_directories`` after them are user-curated,
        so they are tagged global too.
        """
        origins: list[str | None] = [SKILL_ORIGIN_GLOBAL, SKILL_ORIGIN_BUNDLED]
        origins.extend(SKILL_ORIGIN_GLOBAL for _ in scan_roots[2:])
        return origins

    def agent_skills_dir(self, agent_id: str) -> Path:
        """Return an agent's private skill home (``<data_dir>/agents/<id>/skills``)."""
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return self._storage.data_dir / _AGENTS_DIRNAME / agent_id / _SKILLS_DIRNAME

    def agent_owns_private_skill(self, agent_id: str, name: str) -> bool:
        """Whether an Identity Agent's private home currently loads that Skill name."""
        if self._policy is None:
            return False
        environment = self._skill_environment(self._storage.load_environment())
        return (
            find_skill_package_dir(self.agent_skills_dir(agent_id), name, environment) is not None
        )

    @property
    def global_skills_dir(self) -> Path:
        """Return the user-curated global skills directory (``<data_dir>/skills``)."""
        if self._storage is None:
            raise RuntimeError("Storage service not available")
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
        allowlist. This is the single seam every run-time skill consumer (prompt
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
        self._ensure_started()
        if (
            identity_agent_id is not None
            and self._agents.exists(identity_agent_id)
            and (
                project_id is not None
                or self.agent_skills_dir(identity_agent_id).is_dir()
                or self._receives_shared_skills(identity_agent_id)
            )
        ):
            return self._agent_skill_registry(project_id, identity_agent_id)
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
        self._ensure_started()
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
        self._ensure_started()
        project = self._projects.get(project_id)
        bundle = self._project_skill_bundle(project_id)
        allowed_names = set(effective_project_allowed_skills(project, bundle.names))
        return [skill for skill in bundle.registry.list_all() if skill.name in allowed_names]

    def _manager_sources(self) -> list[tuple[Path, str | None, str | None]]:
        self._ensure_started()
        roots = self._skill_scan_roots(self._storage.load_settings(), self._resources_path)
        sources: list[tuple[Path, str | None, str | None]] = [
            (root, origin, None)
            for root, origin in zip(roots, self._bundled_skill_origins(roots), strict=True)
        ]
        sources.extend(
            (
                project_skills_dir(Path(project.cwd), project.source_format),
                project_skill_origin(project.display_name),
                None,
            )
            for project in self._projects.list()
        )
        sources.extend(
            (self.agent_skills_dir(agent.id), SKILL_ORIGIN_AGENT, agent.id)
            for agent in self._agents.list()
        )
        unique_sources: dict[tuple[Path, str | None], tuple[Path, str | None, str | None]] = {}
        for source in sources:
            unique_sources.setdefault((source[0].resolve(), source[2]), source)
        return list(unique_sources.values())

    @staticmethod
    def _manager_entry_id(root: Path, path: Path, owner_id: str | None) -> str:
        identity = f"{root.resolve().as_posix()}\0{path.resolve().as_posix()}\0{owner_id or ''}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def inspect_skill(self, entry_id: str) -> dict[str, Any]:
        """Read exactly one currently inventoried package without activating it."""
        environment = self._skill_environment(self._storage.load_environment())
        for root, _origin, owner_id in self._manager_sources():
            registry = SkillRegistry.load(root, environment=environment)
            paths = [skill.path for skill in registry.list_all()]
            paths.extend(diagnostic.path for diagnostic in registry.invalid_diagnostics())
            for path in paths:
                if self._manager_entry_id(root, path, owner_id) == entry_id:
                    return {"id": entry_id, "content": path.read_text(encoding="utf-8")}
        raise ValueError("Skill is no longer present in the inventory")

    def skill_inventory(self) -> dict[str, Any]:
        """One pass over every Skill source for the human manager (no exclusions).

        Unlike ``skills_for`` this never applies the policy disable switch, so a
        disabled Skill stays visible and manageable here. Every scanned package
        is listed per source — a same-name Skill in two sources appears once per
        origin — annotated with its origin, owner (private homes only), share and
        disable state, availability, and warnings. Availability is resolved
        against one merged registry so cross-source Skill dependencies answer
        exactly like runtime activation does. Stale policy entries (an unknown
        owner or a vanished package) are reported for cleanup, not silently
        dropped.
        """
        self._ensure_started()
        environment = self._skill_environment(self._storage.load_environment())
        policy = self._policy.load()

        # (metadata, origin, owner_id, warnings, loadable) per scanned package.
        raw_entries: list[tuple[SkillMetadata, str | None, str | None, list[str], bool, Path]] = []
        merged_roots: list[Path] = []
        merged_origins: list[str | None] = []

        def add_root(root: Path, origin: str | None, owner_id: str | None) -> None:
            registry = SkillRegistry.load(root, environment=environment)
            for skill in registry.list_all():
                raw_entries.append(
                    (skill, origin, owner_id, registry.warnings_for(skill.name), True, root)
                )
            for diagnostic in registry.invalid_diagnostics():
                placeholder = SkillMetadata(
                    name=diagnostic.name, description="", path=diagnostic.path
                )
                raw_entries.append(
                    (placeholder, origin, owner_id, diagnostic.warnings, False, root)
                )
            merged_roots.append(root)
            merged_origins.append(origin)

        for root, origin, owner_id in self._manager_sources():
            add_root(root, origin, owner_id)

        merged = SkillRegistry.load(
            merged_roots[0],
            extra_dirs=merged_roots[1:],
            environment=environment,
            origins=merged_origins,
        )
        skills: list[dict[str, Any]] = []
        for skill, origin, owner_id, warnings, loadable, root in raw_entries:
            if loadable:
                availability = merged.availability_for(skill.name)
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
            skills.append(
                {
                    "id": self._manager_entry_id(root, skill.path, owner_id),
                    "editable_scope": (
                        f"agent:{owner_id}"
                        if owner_id
                        else "global"
                        if root.resolve() == self.global_skills_dir.resolve()
                        else None
                    )
                    if loadable
                    else None,
                    "source_label": (root.parent.name if root.name == "skills" else root.name)
                    if origin == SKILL_ORIGIN_GLOBAL
                    and root.resolve() != self.global_skills_dir.resolve()
                    else None,
                    "name": skill.name,
                    "description": skill.description,
                    "origin": origin,
                    "owner_id": owner_id,
                    "shared": bool(shared_receivers),
                    "shared_with": sorted(shared_receivers),
                    "disabled": disabled,
                    "status": status,
                    "missing": missing,
                    "optional_missing": optional_missing,
                    "warnings": warnings,
                }
            )
        return {
            "skills": skills,
            "policy_diagnostics": self._policy.validation_diagnostics(),
            "stale_shared": self._stale_shared_entries(policy),
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

    def project_skill_names(self, project_id: str | None) -> frozenset[str]:
        """Return the names of a project's own scanned skills (empty for identity).

        The resolver uses this to compute a config agent's effective skills
        ``(project skills − disabled) ∪ enabled-bundled``. Cached with the project's
        merged registry so it does not re-scan the repo every resolve.
        """
        self._ensure_started()
        if project_id is None:
            return frozenset()
        return self._project_skill_bundle(project_id).names

    def invalidate_project_skills(self, project_id: str | None = None) -> None:
        """Drop the cached project skills for one project, or for all when ``None``.

        Agent-aware registries embed the project layer, so this also drops the
        cached agent registries for that project (or all of them when ``None``) to
        keep them coherent with the project pool.
        """
        if project_id is None:
            self._project_skills.clear()
            self._agent_skills.clear()
            return
        self._project_skills.pop(project_id, None)
        self._drop_agent_skills(lambda key: key[0] == project_id)

    def invalidate_agent_skills(self, agent_id: str | None = None) -> None:
        """Drop the cached agent skills for one agent, or for all when ``None``.

        Called after an agent's private skill home changes (a skill write) so the
        next run rebuilds that agent's registry against the new pool. Drops only
        that agent's cached registries across every project context it ran in.
        """
        if agent_id is None:
            self._agent_skills.clear()
            return
        self._drop_agent_skills(lambda key: key[1] == agent_id)

    def _drop_agent_skills(self, predicate: Callable[[tuple[str | None, str]], bool]) -> None:
        for key in [key for key in self._agent_skills if predicate(key)]:
            del self._agent_skills[key]

    def _agent_skill_registry(self, project_id: str | None, agent_id: str) -> SkillRegistry:
        key = (project_id, agent_id)
        cached = self._agent_skills.get(key)
        if cached is not None:
            return cached
        registry = self._build_agent_skill_registry(project_id, agent_id)
        self._agent_skills[key] = registry
        return registry

    def _build_agent_skill_registry(self, project_id: str | None, agent_id: str) -> SkillRegistry:
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
        origins.extend(self._bundled_skill_origins(scan_roots))
        # First-found-wins ordering makes agent skills win over project, project over
        # shared, shared over bundled. The agent's own skills are always-allowed for
        # it, so they bypass the owner's ``allowed_skills`` filter without leaking to
        # other agents (whose registries never scan this home). Project Context is
        # itself the authorization to use that Project's effective Skill set: those
        # exact Project-granted names also bypass the Identity Agent's unrelated
        # personal allowlist while this project-scoped registry is active.
        agent_own_names = scan_skill_names(agent_root, environment)
        return SkillRegistry.load(
            roots[0],
            extra_dirs=roots[1:],
            environment=environment,
            always_allowed=agent_own_names | project_allowed_names,
            origins=origins,
            excluded_names=self._disabled_skill_names(),
        )

    def _receives_shared_skills(self, receiver_agent_id: str) -> bool:
        """Whether any other Identity Agent has shared Skills to this receiver.

        Part of the ``skills_for`` scoping decision: an agent with no private home
        and no Project must still get a scoped registry when others share to it.
        """
        if self._policy is None:
            return False
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
        if self._policy is None:
            return None
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
        if self._policy is None:
            return []
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
        cached = self._project_skills.get(project_id)
        if cached is not None:
            return cached
        bundle = self._build_project_skill_bundle(project_id)
        self._project_skills[project_id] = bundle
        return bundle

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
            bundled_origins=self._bundled_skill_origins(scan_roots),
            excluded_names=disabled,
        )
        # The resolver's config-agent input must be clean of disabled names too —
        # a disabled project skill is invisible everywhere, including opt-ins.
        names = scan_project_skill_names(project_cwd, project.source_format, environment)
        return _ProjectSkillBundle(registry=registry, names=names - disabled)
