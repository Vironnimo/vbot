"""Fresh repository Profiles, Team scans and configuration translation."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.projects._resolution_values import _overridden_model, effective_project_allowed_skills
from core.projects._runtime_agent import AgentResolutionError, ResolutionAgentNotFoundError
from core.projects.scan_report import FindingType, ScanFinding
from core.projects.sources import AgentProfile, ScanResult, Translation, read_profile, scan_project
from core.projects.sources._translation import rules_allow

if TYPE_CHECKING:
    from core.projects.projects import Project
    from core.projects.resolver import AgentResolver


class TeamResolution:
    def __init__(self, owner: AgentResolver) -> None:
        self._owner = owner

    def _preload_profile(
        self, project_id: str, profile: AgentProfile, allowed: list[str]
    ) -> AgentProfile:
        from core.skills.skills import format_skill_activation_context

        body = [profile.body]
        for name in profile.preload_skills:
            if name not in allowed and "*" not in allowed:
                continue
            content = self._owner._profile_skill_content(project_id, name, allowed)
            if content is not None:
                body.append(format_skill_activation_context(name, content))
        return replace(profile, body="\n\n".join(part for part in body if part))

    def scan_project_report(self, project: Project) -> ScanResult:
        """Scan a project into Team + a **complete** report (incl. model + pointer findings).

        This is the project-scoped scan the open/re-scan path uses: it runs the
        structural scan, then appends one ``BAD_MODEL`` finding per config agent
        whose declared model is not configured in this instance and one ``ORPHAN``
        finding per anchor pointer — the project's ``default_agent`` and every
        session-owning agent under the anchor — that names an agent the scan did
        not produce (both via :meth:`ScanReport.with_findings`). Both checks happen
        **here, at scan time** — not lazily at first run.
        """
        result = scan_project(
            _project_root(project),
            adapters=self._owner._source_adapters,
            sources=project.sources,
        )
        translated = [self.translated_profile(project, member) for member in result.team]
        model_findings = self._model_findings(project, translated)
        pointer_findings = self._pointer_findings(project, result.team)
        report = result.report.with_findings(model_findings + pointer_findings)
        return replace(result, team=translated, report=report)

    def team_for_project(self, project_id: str) -> list[AgentProfile]:
        """Return the current cached Team snapshot for one registered Project."""
        return list(self._project_team(self._owner._load_project(project_id)))

    def rescan_project(self, project: Project) -> ScanResult:
        """Re-run the project scan and refresh the cached Team for this project.

        Called at project-open and on an explicit re-scan. Returns the same
        Team + complete report as :meth:`scan_project_report` and updates the
        Team-membership cache so subsequent ``resolve_agent`` calls see the new
        Team without re-walking the repo.
        """
        result = self.scan_project_report(project)
        self._owner._team_cache[project.project_id] = (_team_key(project), result)
        return result

    def invalidate_team_cache(self, project_id: str | None = None) -> None:
        """Drop the cached Team for one project, or for all when ``None``."""
        if project_id is None:
            self._owner._team_cache.clear()
            return
        self._owner._team_cache.pop(project_id, None)

    def cached_scan(self, project: Project) -> ScanResult:
        """Return the cached Team + complete report, scanning only on first use.

        Unlike :meth:`rescan_project` this never re-walks a repository that was
        already scanned, so listing every Project's Team stays cheap; an open or an
        explicit re-scan refreshes the cache.
        """
        cached = self._owner._team_cache.get(project.project_id)
        # A changed Source list, location or Model mapping (a background Source
        # refresh included) makes the cached Team stale.
        if cached is not None and cached[0] == _team_key(project):
            return cached[1]
        # Lazy first scan: a resolve before any explicit open still works, and the
        # result is cached so the next turn does not re-walk the repo.
        return self.rescan_project(project)

    def _project_team(self, project: Project) -> list[AgentProfile]:
        return self.cached_scan(project).team

    def _read_agent_fresh(self, project: Project, agent_id: str) -> AgentProfile:
        """Reload the selected repository definition and its inherited settings.

        Reads the live config from disk so a repo edit is reflected on the next
        run. If the agent vanished from the repo since the cached Team was built
        (deleted file), that is an "agent no longer exists" error rather than a
        silent fall-back to the stale cached profile.
        """
        try:
            # Team resolution has already selected a winner. A temporary Profile
            # may be requested before that scan; keep its ordinary live lookup
            # instead of filling the Team/Skill report just for this read.
            selected: AgentProfile | None = None
            cached = self._owner._team_cache.get(project.project_id)
            if cached is not None and cached[0] == _team_key(project):
                selected = next(
                    (member for member in cached[1].team if member.agent_id == agent_id), None
                )
            member = read_profile(
                _project_root(project),
                project.sources,
                agent_id,
                self._owner._source_adapters,
                selected=selected,
            )
        except (OSError, ValueError) as error:
            raise AgentResolutionError(
                f"Cannot read repository Agent '{agent_id}': {error}"
            ) from error
        if member is not None:
            if member.unavailable_reason:
                raise AgentResolutionError(
                    f"Repository Agent '{agent_id}' is unavailable: {member.unavailable_reason}"
                )
            return member
        raise ResolutionAgentNotFoundError(
            f"agent '{agent_id}' is no longer present in project '{project.project_id}'"
        )

    def _profile_model(self, project: Project, scanned: AgentProfile) -> str:
        if scanned.model == "inherit":
            return ""
        return project.model_mappings.get(scanned.model, scanned.model)

    def translated_profile(self, project: Project, scanned: AgentProfile) -> AgentProfile:
        reports = list(scanned.translations)
        mapped = self._profile_model(project, scanned)
        if scanned.model == "inherit":
            reports = [entry for entry in reports if entry.setting != "model"]
            reports.append(
                Translation(
                    "model",
                    "translated",
                    "Uses the delegating caller's Model, otherwise the Project or global default.",
                )
            )
        elif mapped != scanned.model and self._owner._model_checker.is_configured(mapped):
            reports = [entry for entry in reports if entry.setting != "model"]
            reports.append(
                Translation(
                    "model",
                    "translated",
                    f"Model wish '{scanned.model}' maps to '{mapped}'.",
                )
            )
        if (
            scanned.model
            and scanned.model != "inherit"
            and not self._owner._model_checker.is_configured(mapped)
        ):
            reports = [entry for entry in reports if entry.setting != "model"]
            reports.append(
                Translation(
                    "model",
                    "not_supported",
                    f"Model wish '{scanned.model}' is unmapped or unusable; "
                    "the Project or global default is used.",
                )
            )
        for setting in project.overrides.get(scanned.agent_id, {}):
            reports = [
                replace(
                    entry,
                    status="overridden",
                    detail="The explicit vBot Tool policy replaces this imported restriction.",
                )
                if setting == "tool_access"
                and entry.setting not in {"permission.skill", "permission.task"}
                and entry.setting.startswith(
                    (
                        "tools",
                        "permission",
                        "disallowedTools",
                        "hooks",
                        "sandbox",
                        "approval",
                        "isolation",
                        "readonly",
                    )
                )
                else entry
                for entry in reports
                if entry.setting != setting
                and not (
                    setting == "thinking_effort"
                    and entry.setting in {"effort", "model_reasoning_effort"}
                )
            ]
            reports.append(
                Translation(
                    setting,
                    "overridden",
                    "The explicit vBot override replaces this repository setting.",
                )
            )
        allowed = effective_project_allowed_skills(
            project, self._owner._project_skill_names(project.project_id)
        )
        for name in scanned.preload_skills:
            if name not in allowed or not _profile_skill_allowed(scanned, name):
                reports.append(
                    Translation(
                        f"skills.{name}",
                        "not_supported",
                        "Skill is absent or outside the Project Skill selection; it cannot"
                        " be preloaded.",
                    )
                )
            elif self._owner._profile_skill_content(project.project_id, name, allowed) is None:
                reports.append(
                    Translation(
                        f"skills.{name}",
                        "not_supported",
                        "Skill is unavailable or unreadable; it cannot be preloaded.",
                    )
                )
        return replace(scanned, translations=tuple(reports))

    def _model_findings(self, project: Project, team: list[AgentProfile]) -> list[ScanFinding]:
        return [
            _bad_model_finding(member)
            for member in team
            if member.model
            and member.model != "inherit"
            and not self._owner._model_checker.is_configured(
                _overridden_model(project, member.agent_id)
            )
            and not self._owner._model_checker.is_configured(self._profile_model(project, member))
        ]

    def _pointer_findings(self, project: Project, team: list[AgentProfile]) -> list[ScanFinding]:
        """Build the scan's ``ORPHAN`` findings for the project's anchor pointers.

        The pointers live in the anchor, not the repo, so the structural scan
        cannot see them: the project's ``default_agent`` and every session-owning
        agent under the anchor must name an agent the current scan produced. A
        pointer at an id the scan did not yield is unclean under what exists — the
        default agent cannot resolve, and orphaned sessions belong to an agent that
        is no longer in the repo (renamed or deleted). A project without a default
        agent and without sessions yields no findings (clean empty is normal).
        """
        team_ids = {member.agent_id for member in team}
        findings: list[ScanFinding] = []
        if project.default_agent and project.default_agent not in team_ids:
            findings.append(
                _orphan_finding(
                    project.default_agent,
                    f"default agent '{project.default_agent}' is not in the scanned team",
                )
            )
        for owner in self._owner._projects.session_owning_agents(project.project_id):
            if owner not in team_ids:
                findings.append(
                    _orphan_finding(
                        owner,
                        f"agent '{owner}' owns sessions in this project "
                        f"but is not in the scanned team",
                    )
                )
        return findings


def _profile_skill_allowed(profile: AgentProfile, name: str) -> bool:
    return rules_allow(profile.skill_rules, name)


def _bad_model_finding(member: AgentProfile) -> ScanFinding:
    """Build a ``BAD_MODEL`` finding for a scanned agent's unconfigured model."""
    return ScanFinding(
        type=FindingType.BAD_MODEL,
        detail=(
            f"model '{member.model}' is not configured in this instance "
            f"(unknown provider/model or no usable connection)"
        ),
        agent_id=member.agent_id,
        source_path=member.source_path,
    )


def _orphan_finding(agent_id: str, detail: str) -> ScanFinding:
    """Build an ``ORPHAN`` finding for an anchor pointer at a non-team agent id.

    Pointer-origin findings carry no ``source_path`` — the pointer lives in the
    anchor (``project.json`` / the sessions subtree), not in a repo source file.
    """
    return ScanFinding(type=FindingType.ORPHAN, detail=detail, agent_id=agent_id)


def _project_root(project: Project) -> Path:
    """Return the repo root a project's scan runs against (its cwd)."""
    return Path(project.cwd)


def _team_key(project: Project) -> Hashable:
    return (project.cwd, tuple(project.sources), tuple(sorted(project.model_mappings.items())))
