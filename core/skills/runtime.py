"""Skill scope resolution, inventory, sharing, and registry caches."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.agents import AgentStore
from core.extensions import ExtensionRegistry
from core.projects import (
    Project,
    ProjectNotFoundError,
    ProjectStore,
    effective_project_allowed_skills,
)
from core.skills._global_scanning import (
    _AGENTS_DIRNAME,
    _SKILLS_DIRNAME,
    _global_roots,
    _GlobalRoot,
    _origin_layers,
    _package_exclusions,
    load_global_skill_registry,
)
from core.skills._inventory import SkillInventory
from core.skills.authoring import (
    ArchivedSkill,
    SkillAuthoringService,
    SkillMergeAccessError,
    SkillWriter,
    SkillWriteResult,
)
from core.skills.policy import SkillPackageRef, SkillPolicy, SkillPolicyService
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SKILL_ORIGIN_PROJECT_PREFIX,
    SkillMetadata,
    SkillRegistry,
    SkillScan,
    find_skill_package_dir,
    load_project_skill_registry,
    project_skill_origin,
    scan_project_skill_names,
)
from core.storage import StorageManager
from core.utils.log_conditions import LoggedConditions


@dataclass(frozen=True)
class _ProjectSkillBundle:
    registry: SkillRegistry
    names: frozenset[str]
    # The Project record the bundle was built from, Sources included.
    project: Project

    def same_as(self, other: _ProjectSkillBundle) -> bool:
        """Whether ``other`` leaves this bundle and its dependent registries current."""
        return (
            self.project == other.project
            and self.names == other.names
            and self.registry.same_catalog(other.registry)
        )


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
        authoring: SkillAuthoringService | None = None,
    ) -> None:
        self._skills = registry
        # Answers provenance, pins and archives of the writable homes; without it
        # the inventory carries no records and no archive.
        self._authoring = authoring
        self._policy = policy
        self._storage = storage
        self._agents = agents
        self._get_projects = projects
        self._extensions = extensions
        self._resources_path = resources_path
        self._logger = logger
        self._reload = reload_skills
        # Every receiver's registry build meets the same stale policy entries:
        # warn once per entry until it is resolved, not once per build.
        self._stale_shared = LoggedConditions()
        self._project_skills: dict[str, _ProjectSkillBundle] = {}
        self._agent_skills: dict[tuple[str | None, str], SkillRegistry] = {}
        # Scans run in workers as well as on the Event Loop. Hold this lock only
        # around cache metadata; mutations must never wait for filesystem scans.
        self._cache_lock = threading.RLock()
        self._cache_generation = 0
        self._changed_callbacks: list[Callable[[], None]] = []
        self._inventory = SkillInventory(self)

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
            disabled_packages=self._policy.load().disabled_packages,
            logger=self._logger,
        )

    def _disabled_packages(self, global_roots: list[_GlobalRoot]) -> frozenset[tuple[Path, str]]:
        """The (Skill home, name) pairs of every package the Skill Policy turns off."""
        return _package_exclusions(
            global_roots, self.agent_skills_dir, self._policy.load().disabled_packages
        )

    def _skill_environment(self, fallback_environment: dict[str, str]) -> dict[str, str]:
        environment = dict(fallback_environment)
        environment.update(os.environ)
        return environment

    def _global_roots(self, settings: dict[str, object]) -> list[_GlobalRoot]:
        return _global_roots(
            self._storage, self._resources_path, settings, self._extensions, self._logger
        )

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

    def archived_skill(self, agent_id: str | None, name: str) -> ArchivedSkill | None:
        """Return the archived Skill *name* of the homes a Run sees, own home first.

        ``agent_id`` names an Identity Agent's own home; the global home is always
        searched.
        """
        if self._authoring is None:
            return None
        homes = [self.global_skills_dir]
        if agent_id is not None:
            homes.insert(0, self.agent_skills_dir(agent_id))
        for home in homes:
            archived = self._authoring.archived_skill(home, name)
            if archived is not None:
                return archived
        return None

    def shared_skill_receivers(self, owner_id: str) -> dict[str, frozenset[str]]:
        """Map each Skill of ``owner_id``'s private home that it shares to its receiver Agents.

        Skills shared with nobody but the owner are absent. Librarian aging counts
        the receivers' use of a shared Skill as use of the owner's Skill.
        """
        shared = self._policy.load().shared.get(owner_id, {})
        return {
            name: frozenset(receivers - {owner_id})
            for name, receivers in shared.items()
            if receivers - {owner_id}
        }

    @contextmanager
    def merge_guard(self) -> Iterator[None]:
        """Keep the access snapshot and source deletion in one synchronous write.

        Lock order is Agent lifecycle, Project configuration, Skill authoring,
        then policy. Follow-up references still use their own
        writes after this section; this is not a cross-store transaction.
        """
        if self._authoring is None:
            raise RuntimeError("Skill authoring is not configured")
        with (
            self._agents.lifecycle_guard(),
            self._projects.configuration_guard(),
            self._authoring.exclusive(),
            self._policy.exclusive(),
        ):
            yield

    def prepare_skill_merge(self, owner_id: str, name: str, target: str) -> tuple[str, ...]:
        """Refuse lost access before a merge, otherwise snapshot its actual users.

        Build the same registries as activation with the proposed share/selection
        changes, without persisting them. Every registered Project Context is a
        scope an Identity Agent can load; only contexts that can currently load
        this exact source package require access to its exact successor.
        """
        with self._agents.lifecycle_guard():
            scan = SkillScan()
            own = SkillRegistry.load(self.agent_skills_dir(owner_id), scan=scan)
            try:
                document = own.get(name).path
                successor = own.get(target).path
            except KeyError:
                # The authoring Tool reports an absent source or invalid target.
                return ()
            policy = self._policy.load()
            owner_shares = dict(policy.shared.get(owner_id, {}))
            receivers = owner_shares.pop(name, frozenset())
            owner_shares[target] = owner_shares.get(target, frozenset()) | receivers
            proposed = replace(
                policy,
                shared={**policy.shared, owner_id: owner_shares},
                # Simulate deletion for name resolution and Skill dependencies.
                disabled_packages=policy.disabled_packages
                | {SkillPackageRef("agent", name, owner_id)},
            )
            projects: list[Project | None] = [None]
            projects.extend(
                self._projects.refresh_sources(project.project_id, skill_scan=scan)
                for project in self._projects.list()
            )
            permitted: set[str] = set()
            blocked: list[tuple[str, str | None, str]] = []
            for agent_id in sorted({owner_id, *receivers}):
                agent = self._agents.find(agent_id)
                if agent is None or agent.builtin is not None:
                    continue
                exclusions = frozenset(agent.excluded_skills)
                allowed = agent.allowed_skills
                proposed_allowed = None if allowed is None else [*allowed, target]
                for project in projects:
                    project_id = project.project_id if project is not None else None
                    bundle = (
                        self._build_project_skill_bundle(project_id, project=project, scan=scan)
                        if project_id is not None
                        else None
                    )
                    registry = self._build_agent_skill_registry(
                        project_id,
                        agent_id,
                        exclusions,
                        project=project,
                        project_bundle=bundle,
                        scan=scan,
                        policy=policy,
                    )
                    if not self._can_load_package(registry, name, document, allowed):
                        continue
                    permitted.add(agent_id)
                    after = self._build_agent_skill_registry(
                        project_id,
                        agent_id,
                        exclusions - {target},
                        project=project,
                        project_bundle=bundle,
                        scan=scan,
                        policy=proposed,
                        removed_own_names=frozenset({name})
                        if agent_id == owner_id
                        else frozenset(),
                    )
                    if self._can_load_package(after, target, successor, proposed_allowed):
                        continue
                    if SkillPackageRef("agent", target, owner_id) in policy.disabled_packages:
                        reason = "disabled"
                    elif any(
                        skill.name == target and skill.path != successor
                        for skill in after.list_all()
                    ):
                        reason = "shadowed"
                    else:
                        reason = "unavailable"
                    blocked.append(
                        (agent.name or agent_id, project.display_name if project else None, reason)
                    )
                    # One inaccessible context is enough to refuse this Agent's
                    # merge; repeating it adds no recovery information.
                    break
            if blocked:
                raise SkillMergeAccessError(blocked)
            return tuple(sorted(permitted))

    @staticmethod
    def _can_load_package(
        registry: SkillRegistry, name: str, path: Path, allowed: list[str] | None
    ) -> bool:
        try:
            skill = registry.get(name)
        except KeyError:
            return False
        return (
            skill.path == path
            and registry.is_allowed(name, allowed)
            and registry.availability_for(name, allowed).state == "available"
        )

    def preserve_merged_skill_access(self, agent_id: str, target: str) -> None:
        """Keep a prior grant after the Skill's share moved to its successor."""
        with self._agents.lifecycle_guard():
            agent = self._agents.get(agent_id)
            changes: dict[str, Any] = {}
            allowed = agent.allowed_skills
            if allowed is not None and "*" not in allowed and target not in allowed:
                changes["allowed_skills"] = [*allowed, target]
            if target in agent.excluded_skills:
                changes["excluded_skills"] = [
                    name for name in agent.excluded_skills if name != target
                ]
            if changes:
                self._agents.update(agent_id, **changes)
                self.invalidate_agent_skills(agent_id)

    def background_protection(self, agent_id: str, names: Iterable[str]) -> dict[str, str]:
        """Return why a background Run of *agent_id* cannot change each named Skill.

        Names are Skills the Agent sees as its own: its private Skills and those
        other Agents share with it. The reason is ``pinned`` or ``unknown`` for
        the package that owns the name (see
        ``SkillAuthoringService.background_protection``). Names a background Run
        can change are absent.
        """
        if self._authoring is None:
            return {}
        names = tuple(names)
        if not names:
            return {}
        environment = self._skill_environment(self._storage.load_environment())
        homes: dict[Path, dict[str, str]] = {}
        protection: dict[str, str] = {}
        own_root = self.agent_skills_dir(agent_id)
        own_packages = {
            skill.name: skill.path.parent
            for skill in SkillRegistry.load(
                own_root, environment=environment, excluded_packages=self._disabled_packages([])
            ).list_all()
        }
        shared_packages = self._resolve_shared_skill_packages(
            agent_id, set(names).difference(own_packages)
        )
        for name in names:
            home = own_root
            package = own_packages.get(name)
            if package is None:
                package = shared_packages.get(name)
                if package is None:
                    continue
                home = package.parent
            if home not in homes:
                homes[home] = dict(self._authoring.background_protection(home))
            reason = homes[home].get(package.name)
            if reason is not None:
                protection[name] = reason
        return protection

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
        ``allowed_skills`` grants and from its own Skills in that scoped registry,
        never from the Project grant; an Agent with exclusions always gets a scoped
        registry.
        This is the single seam every run-time skill consumer (prompt
        assembly, triggers, the ``skill`` tool, autocomplete) resolves through, so
        scoping lives in exactly one place.

        **Contract:** ``identity_agent_id`` carries the run's agent id only when the
        run executes as an identity agent (a run whose Session works in a Project
        passes that Project as ``project_id``). A config-agent run passes ``None``: config
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

        Scans only the Project's enabled repository Skill directories (the Source
        location), so the result is exactly the Project-owned Skills with their
        ``SKILL.md`` paths. The Project Tool lists them in its persisted result, and
        Chat routes later Skill activation through that loaded Project context. A
        missing directory yields an empty list.
        """
        project = self._projects.refresh_sources(project_id)
        environment = self._skill_environment(self._storage.load_environment())
        registry = load_project_skill_registry(project.skill_roots, [], environment)
        return registry.list_all()

    def project_context_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return the complete effective Skill set carried by Project Context.

        Project-owned Skills are active by default except explicit Project
        disables; bundled and global Skills join only through the Project's opt-in
        lists. This is the same Project policy used for Config Agents and the
        temporary Project grant applied to Identity Runs.
        """
        project = self._projects.refresh_sources(project_id)
        bundle = self._project_skill_bundle(project_id)
        allowed_names = set(effective_project_allowed_skills(project, bundle.names))
        return [skill for skill in bundle.registry.list_all() if skill.name in allowed_names]

    def inspect_skill(self, entry_id: str) -> dict[str, Any]:
        """Read exactly one currently inventoried package without activating it."""
        return self._inventory.inspect_skill(entry_id)

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
        return self._inventory.set_package_disabled(entry_id, disabled=disabled)

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
        return self._inventory.skill_inventory()

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
        return self._classify_project_skills(bundle)

    @staticmethod
    def _classify_project_skills(bundle: _ProjectSkillBundle) -> dict[str, list[SkillMetadata]]:
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

    def refresh_project_skills(self, project: Project) -> None:
        """Rescan one Project's Skills; keep its caches when the rescan changes nothing.

        ``project`` is the freshly read record with current Sources. A rescan equal
        to the cached one (same record, Skill names and catalog) keeps that bundle
        and the Agent registries embedding it, so repeated opens neither rebuild
        them nor discard scoped scans running meanwhile. Otherwise the rescan
        replaces the bundle and those Agent registries rebuild, as after
        :meth:`invalidate_project_skills`.
        """
        project_id = project.project_id
        with self._cache_lock:
            generation = self._cache_generation
            cached = self._project_skills.get(project_id)
        fresh = self._build_project_skill_bundle(project_id, project=project)
        with self._cache_lock:
            if generation != self._cache_generation:
                # Caches were invalidated during the rescan, which may predate that.
                self.invalidate_project_skills(project_id)
            elif cached is None or not cached.same_as(fresh):
                self._cache_generation += 1
                self._project_skills[project_id] = fresh
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
        *,
        project: Project | None = None,
        project_bundle: _ProjectSkillBundle | None = None,
        scan: SkillScan | None = None,
        policy: SkillPolicy | None = None,
        removed_own_names: frozenset[str] = frozenset(),
    ) -> SkillRegistry:
        scan = scan or SkillScan()
        policy = policy or self._policy.load()
        settings = self._storage.load_settings()
        environment = self._skill_environment(self._storage.load_environment())
        agent_root = self.agent_skills_dir(agent_id)
        global_roots = self._global_roots(settings)
        scan_roots = [root.root for root in global_roots]
        roots: list[Path] = [agent_root]
        origins: list[str | None] = [SKILL_ORIGIN_AGENT]
        package_sources: list[tuple[str, str | None]] = [("agent", agent_id)]
        project_allowed_names: set[str] = set()
        if project_id is not None:
            project = project or self._projects.refresh_sources(project_id, skill_scan=scan)
            roots.extend(project.skill_roots)
            package_sources.extend(
                ("folder", root.resolve().as_posix()) for root in project.skill_roots
            )
            origins.extend([project_skill_origin(project.display_name)] * len(project.skill_roots))
            project_allowed_names.update(
                effective_project_allowed_skills(
                    project,
                    (project_bundle or self._project_skill_bundle(project_id)).names,
                )
            )
        # Shared layer (own > project > shared > global > bundled): every other
        # owner's individually resolved shared package directories — never an
        # owner's whole skills home, so unshared neighbours cannot leak. They are
        # tagged with the receiver-facing Agent origin, so catalogs render them
        # indistinguishably among "Your own skills", and they are NOT added to
        # ``always_allowed``: they pass through the receiver's ``allowed_skills``
        # filter exactly like global skills.
        shared_package_dirs = self._shared_package_dirs(agent_id, scan=scan, policy=policy)
        roots.extend(shared_package_dirs)
        package_sources.extend(
            ("agent", package.parent.parent.name) for package in shared_package_dirs
        )
        origins.extend(SKILL_ORIGIN_AGENT for _ in shared_package_dirs)
        roots.extend(scan_roots)
        origins.extend(_origin_layers(scan_roots))
        package_sources.extend((root.kind, root.location) for root in global_roots)
        # First-found-wins ordering makes agent skills win over project, project over
        # shared, shared over bundled. The agent's own skills are always-allowed for
        # it unless its ``excluded_skills`` turn them off, so they bypass the owner's
        # ``allowed_skills`` filter without leaking to other agents (whose registries
        # never scan this home). Project Context is itself the authorization to use
        # that Project's effective Skill set: those exact Project-granted names also
        # bypass the Identity Agent's unrelated personal allowlist and its exclusions
        # while this project-scoped registry is active.
        excluded_packages = _package_exclusions(
            global_roots, self.agent_skills_dir, policy.disabled_packages
        )
        # A disabled private package grants nothing to a same-named replacement.
        agent_own_names = (
            frozenset(
                skill.name
                for skill in SkillRegistry.load(
                    agent_root,
                    environment=environment,
                    excluded_packages=excluded_packages,
                    scan=scan,
                ).list_all()
            )
            - removed_own_names
        )
        return SkillRegistry.load(
            roots[0],
            extra_dirs=roots[1:],
            environment=environment,
            always_allowed=(agent_own_names - exclusions) | project_allowed_names,
            origins=origins,
            package_sources=package_sources,
            excluded_packages=excluded_packages,
            allowlist_exclusions=exclusions,
            scan=scan,
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
        package = self._resolve_shared_skill_packages(receiver_agent_id, [name]).get(name)
        return package.parent if package is not None else None

    def _resolve_shared_skill_packages(
        self, receiver_agent_id: str, names: Iterable[str]
    ) -> dict[str, Path]:
        """Resolve named shared packages with at most one scan of each owner.

        This lookup is local to one operation, so authoring and explicit refreshes
        keep their existing freshness and cache-invalidation boundaries.
        """
        unresolved = set(names)
        if not unresolved:
            return {}
        policy = self._policy.load()
        shared = policy.shared
        if not shared:
            return {}
        disabled = _package_exclusions([], self.agent_skills_dir, policy.disabled_packages)
        environment = self._skill_environment(self._storage.load_environment())
        packages: dict[str, Path] = {}
        for owner_id, skills in sorted(shared.items()):
            if owner_id == receiver_agent_id:
                continue
            candidates = [
                name
                for name in sorted(unresolved)
                if receiver_agent_id in skills.get(name, frozenset())
            ]
            if not candidates or not self._agents.exists(owner_id):
                continue
            registry = SkillRegistry.load(
                self.agent_skills_dir(owner_id),
                environment=environment,
                excluded_packages=disabled,
            )
            for name in candidates:
                try:
                    packages[name] = registry.get(name).path.parent
                except KeyError:
                    continue
                unresolved.remove(name)
            if not unresolved:
                break
        return packages

    def _resolve_external_skill_scope(
        self, agent_id: str, name: str, project_id: str | None
    ) -> str | None:
        """Return where ``name`` resolves in the caller's live Skill pool.

        The agent-scoped registry is the same seam ``skill`` resolves through, so
        this answers how the name is *visible* to the agent (bundled / global /
        project / own / shared) — never how the authoring core sees it, which only knows
        the target root. ``own`` identifies the caller's package; another Agent
        origin means a Skill shared into this agent. ``global`` is a package in the global home
        (``<data_dir>/skills``), which an attended Agent can change, while
        ``external`` is a global Skill from a configured skill folder or an
        Extension. ``None`` means genuinely unknown, which includes a name that
        only a Project which does not exist could provide.
        """
        try:
            skill = self.skills_for(project_id, agent_id).get(name)
        except KeyError, ProjectNotFoundError:
            return None
        origin = skill.origin
        if origin == SKILL_ORIGIN_AGENT:
            return (
                "own"
                if skill.path.parent.parent == self.agent_skills_dir(agent_id).resolve()
                else "shared"
            )
        if origin == SKILL_ORIGIN_BUNDLED:
            return "bundled"
        if origin == SKILL_ORIGIN_GLOBAL:
            home = self.global_skills_dir.resolve()
            return "global" if skill.path.parent.parent == home else "external"
        if origin is not None and origin.startswith(SKILL_ORIGIN_PROJECT_PREFIX):
            return "project"
        return None

    def publish_agent_skill(
        self, agent_id: str, name: str, *, writer: SkillWriter
    ) -> SkillWriteResult:
        """Move an Identity Agent's private Skill into the global home.

        The private package goes into the Agent's Skill Archive with the reason
        ``published``. What pointed at the private package follows it: its shares
        end (receivers reach the global Skill through their own selection), a
        turned-off private package stays off as the global package, and an Agent
        whose ``allowed_skills`` lists names without ``*`` gets the name, so the
        Agent keeps the Skill it had always allowed as its own. The caller
        reloads the global Skills.
        """
        if self._authoring is None:
            raise RuntimeError("Skill authoring is not configured")
        result = self._authoring.publish(
            self.agent_skills_dir(agent_id), self.global_skills_dir, name, writer=writer
        )
        policy = self._policy.load()
        private = SkillPackageRef("agent", name, agent_id)
        if private in policy.disabled_packages:
            self._policy.set_package_disabled(SkillPackageRef("home", name), disabled=True)
            self._policy.set_package_disabled(private, disabled=False)
        if name in policy.shared.get(agent_id, {}):
            self._policy.set_shared(agent_id, name, shared=False)
        allowed = self._agents.get(agent_id).allowed_skills
        if allowed is not None and "*" not in allowed and name not in allowed:
            self._agents.update(agent_id, allowed_skills=[*allowed, name])
        self.invalidate_agent_skills(None)
        return result

    def _shared_package_dirs(
        self,
        receiver_agent_id: str,
        *,
        scan: SkillScan | None = None,
        policy: SkillPolicy | None = None,
    ) -> list[Path]:
        """Resolve every owner's shared private Skill packages for one receiver.

        Deterministic order — sorted by owner id, then skill name — so first-found
        collision handling matches activation exactly. Only existing Identity
        Agents contribute; stale entries (an unknown owner id or a vanished package
        directory) are ignored at load with a warning, once per entry until it
        resolves, and stay in the policy file for the human manager to clean up.
        Only skills whose receiver list
        includes this receiver are inserted.
        """
        shared = (policy or self._policy.load()).shared
        if not shared:
            return []
        environment = self._skill_environment(self._storage.load_environment())
        directories: list[Path] = []
        for owner_id, skills in sorted(shared.items()):
            if owner_id == receiver_agent_id:
                # The owner keeps its own copy via its private-home layer.
                continue
            if not self._agents.exists(owner_id):
                if self._stale_shared.started(("owner", owner_id)) and self._logger is not None:
                    self._logger.warning(
                        "Ignored shared skills of an unknown agent (owner=%s)", owner_id
                    )
                continue
            self._stale_shared.ended(("owner", owner_id))
            owner_root = self.agent_skills_dir(owner_id)
            owner_registry: SkillRegistry | None = None
            for name, receivers in sorted(skills.items()):
                if receiver_agent_id not in receivers:
                    continue
                if owner_registry is None:
                    owner_registry = SkillRegistry.load(
                        owner_root, environment=environment, scan=scan
                    )
                entry = ("skill", owner_id, name)
                try:
                    package_dir = owner_registry.get(name).path.parent
                except KeyError:
                    if self._stale_shared.started(entry) and self._logger is not None:
                        self._logger.warning(
                            "Ignored shared skill without a private package (owner=%s skill=%s)",
                            owner_id,
                            name,
                        )
                    continue
                self._stale_shared.ended(entry)
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

    def _build_project_skill_bundle(
        self, project_id: str, *, project: Project | None = None, scan: SkillScan | None = None
    ) -> _ProjectSkillBundle:
        scan = scan or SkillScan()
        project = project or self._projects.refresh_sources(project_id, skill_scan=scan)
        settings = self._storage.load_settings()
        global_roots = self._global_roots(settings)
        scan_roots = [root.root for root in global_roots]
        environment = self._skill_environment(self._storage.load_environment())
        registry = load_project_skill_registry(
            project.skill_roots,
            scan_roots,
            environment,
            project_origin=project_skill_origin(project.display_name),
            bundled_origins=_origin_layers(scan_roots),
            bundled_package_sources=[(root.kind, root.location) for root in global_roots],
            excluded_packages=self._disabled_packages(global_roots),
            scan=scan,
        )
        # A Project's own Skills are turned off by the Project itself
        # (``skills_project_disabled``), never by the Skill Policy.
        names = scan_project_skill_names(project.skill_roots, environment, scan=scan)
        return _ProjectSkillBundle(registry=registry, names=names, project=project)


__all__ = ["load_global_skill_registry"]
