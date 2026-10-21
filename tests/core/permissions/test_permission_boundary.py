"""Contract tests of the Permission Boundary: decisions, protected locations, ceilings."""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from core.permissions import (
    BoundaryContext,
    PathAccess,
    PermissionBoundary,
    PermissionCeiling,
    PermissionDenied,
    PermissionRulesError,
    normalize_permission_rules,
)
from tests.directory_links import link_directory

WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Area:
    """A working directory, an outside folder and a vBot data directory on disk."""

    root: Path
    work: Path
    outside: Path
    home: Path
    data: Path
    workspace: Path
    agent_json: Path
    scratch: Path
    skills: Path
    install: Path

    def context(self, **changes: object) -> BoundaryContext:
        context = BoundaryContext(
            workdir=self.work,
            home=self.home,
            data_dir=self.data,
            scratch_root=self.scratch,
            workspace=self.workspace,
            agent_config=self.agent_json,
            readable_dirs=(self.skills,),
            install_dirs=(self.install,),
            loopback_port=22333,
        )
        return replace(context, **changes)  # type: ignore[arg-type]

    def boundary(self, rules: object, **changes: object) -> PermissionBoundary:
        return PermissionBoundary.build(normalize_permission_rules(rules), self.context(**changes))

    def path(self, template: str) -> str:
        names = {
            "{work}": self.work,
            "{outside}": self.outside,
            "{home}": self.home,
            "{data}": self.data,
            "{workspace}": self.workspace,
            "{agent_json}": self.agent_json,
            "{scratch}": self.scratch,
            "{skills}": self.skills,
            "{install}": self.install,
        }
        for name, value in names.items():
            if template.startswith(name):
                return str(value) + template[len(name) :]
        return template


@pytest.fixture
def area(tmp_path: Path) -> Area:
    data = tmp_path / "data"
    workspace = data / "agents" / "a1" / "workspace"
    paths = Area(
        root=tmp_path,
        work=tmp_path / "work",
        outside=tmp_path / "outside",
        home=tmp_path / "home",
        data=data,
        workspace=workspace,
        agent_json=workspace.parent / "agent.json",
        scratch=data / "artifacts" / "temp" / "scratch" / "c1",
        skills=data / "skills",
        install=tmp_path / "install",
    )
    for folder in (
        paths.work / "secrets",
        paths.outside,
        paths.home / "docs",
        paths.workspace,
        paths.scratch,
        paths.skills,
        paths.install,
    ):
        folder.mkdir(parents=True)
    paths.agent_json.write_text("{}", encoding="utf-8")
    return paths


def test_full_access_does_no_work(monkeypatch: pytest.MonkeyPatch, area: Area) -> None:
    boundary = PermissionBoundary.build(None, area.context())

    def touched(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("an unrestricted check touched the filesystem")

    monkeypatch.setattr(os.path, "realpath", touched)

    assert boundary is PermissionBoundary.UNRESTRICTED
    assert not boundary.restricted
    assert boundary.check_path(area.path("{data}/settings.json"), "write") is None
    assert boundary.check_url("http://127.0.0.1:22333/api/rpc") is None
    assert boundary.to_ceiling_dict(source="parent") is None


_WORK_AREA_RULES = {
    "read": "work_area",
    "write": "work_area",
    "allow": ["Read({home}/docs/**)", "Edit(~/drafts)"],
    "deny": ["Read({workdir}/secrets/**)", "Edit({workdir}/locked.txt)"],
}


@pytest.mark.parametrize(
    ("path", "access", "reason"),
    [
        pytest.param("a.txt", "read", None, id="read-inside"),
        pytest.param("a.txt", "write", None, id="write-inside"),
        pytest.param("a.txt", "start_dir", None, id="start-inside"),
        pytest.param("secrets/key", "read", "deny_rule", id="read-deny"),
        pytest.param("secrets/key", "write", "deny_rule", id="read-deny-covers-write"),
        pytest.param("locked.txt", "read", None, id="edit-deny-leaves-read"),
        pytest.param("locked.txt", "write", "deny_rule", id="edit-deny"),
        pytest.param("{home}/docs/d.md", "read", None, id="read-allow"),
        pytest.param("{home}/docs/d.md", "write", "not_permitted", id="read-allow-not-write"),
        pytest.param("{home}/drafts/x.md", "read", None, id="edit-allow-covers-read"),
        pytest.param("{home}/drafts/x.md", "write", None, id="edit-allow"),
        pytest.param("../outside/z", "read", "not_permitted", id="outside"),
        pytest.param("{outside}", "start_dir", "not_permitted", id="start-outside"),
        pytest.param("{scratch}/s.txt", "write", None, id="scratch-writable"),
        pytest.param("{workspace}/MEMORY.md", "read", None, id="workspace-readable"),
        pytest.param("{workspace}/MEMORY.md", "write", "protected_location", id="workspace"),
        pytest.param("{skills}/x/SKILL.md", "read", None, id="skills-readable"),
        pytest.param("{skills}/x/SKILL.md", "write", "protected_location", id="skills"),
        pytest.param("{data}/settings.json", "read", "protected_location", id="data-dir"),
        pytest.param("{agent_json}", "read", "protected_location", id="agent-json"),
        pytest.param("{install}/core/a.py", "write", "protected_location", id="installation"),
    ],
)
def test_path_decisions(area: Area, path: str, access: PathAccess, reason: str | None) -> None:
    refusal = area.boundary(_WORK_AREA_RULES).check_path(area.path(path), access)

    assert (refusal.reason if refusal else None) == reason
    if refusal is not None:
        assert refusal.access == access
        assert "\\" not in refusal.target


@pytest.mark.parametrize(
    ("path", "access", "glob", "pruned", "reason"),
    [
        pytest.param("src", "write", None, (), None, id="nothing-protected-below"),
        pytest.param(".", "write", None, (), "deny_rule", id="deny-rule-below"),
        pytest.param(".", "read", None, (), "deny_rule", id="read-deny-below"),
        pytest.param(".", "read", "*.md", (), None, id="glob-misses-the-denied-folder"),
        pytest.param(".", "read", "s*/key", (), "deny_rule", id="glob-reaches-the-denied-folder"),
        # Windows matches names case-insensitively, wildcard segments included.
        pytest.param(
            ".",
            "read",
            "SEC*/key",
            (),
            "deny_rule" if WINDOWS else None,
            id="glob-case-follows-the-platform",
        ),
        pytest.param(".", "read", None, ("secrets",), None, id="denied-folder-left-out"),
        pytest.param("{data}/..", "read", None, (), "protected_location", id="data-dir-below"),
        pytest.param("{root}", "write", None, (), "protected_location", id="installation-below"),
    ],
)
def test_a_subtree_access_is_refused_for_what_lies_below(
    area: Area,
    path: str,
    access: PathAccess,
    glob: str | None,
    pruned: tuple[str, ...],
    reason: str | None,
) -> None:
    rules = {"deny": ["Read({workdir}/secrets/**)", "Edit({workdir}/*.lock)"]}
    target = str(area.root) if path == "{root}" else area.path(path)
    boundary = area.boundary(rules)

    refusal = boundary.check_path(target, access, subtree=True, glob=glob, pruned=pruned)

    assert (refusal.reason if refusal else None) == reason
    if reason == "deny_rule":
        # The refusal names the denied part it would reach.
        assert refusal is not None and refusal.below is not None
        assert "/secrets" in refusal.below or refusal.below.endswith(".lock")
    # Without subtree the folder itself is permitted.
    assert boundary.check_path(target, access) is None


@pytest.mark.parametrize(
    "rules",
    [
        pytest.param({}, id="empty"),
        pytest.param(
            {
                "read": "anywhere",
                "write": "anywhere",
                "commands": "any",
                "web": "any",
                "allow": [],
                "deny": [],
            },
            id="explicit-full-access",
        ),
    ],
)
def test_rules_that_restrict_nothing_are_full_access(area: Area, rules: dict[str, object]) -> None:
    # Full-access Agents keep managing vBot: no protected data directory, no refused CLI.
    assert area.boundary(rules) is PermissionBoundary.UNRESTRICTED


def test_a_restriction_elsewhere_keeps_full_reach_but_protects_vbot(area: Area) -> None:
    boundary = area.boundary({"web": "listed"})

    assert boundary.restricted
    assert boundary.check_path(area.path("{outside}/x"), "write") is None
    refusal = boundary.check_path(area.path("{data}/settings.json"), "write")
    assert refusal is not None and refusal.location == "data_dir"
    refusal = boundary.check_path(area.path("{agent_json}"), "read")
    assert refusal is not None and refusal.location == "agent_config"
    refusal = boundary.check_path(area.path("{install}/vbot.exe"), "write")
    assert refusal is not None and refusal.location == "installation"
    assert boundary.check_path(area.path("{install}/vbot.exe"), "read") is None


def test_scratch_base_keeps_the_working_directory_read_only(area: Area) -> None:
    boundary = area.boundary({"read": "work_area", "write": "scratch"})

    assert boundary.check_path("a.txt", "read") is None
    refusal = boundary.check_path("a.txt", "write")
    assert refusal is not None and refusal.reason == "not_permitted" and refusal.base == "scratch"
    assert boundary.check_path(area.path("{scratch}/out.txt"), "write") is None
    assert boundary.allowed_summary("write") == (area.scratch.as_posix(),)


def test_confined_reading_confines_writing_too(area: Area) -> None:
    boundary = area.boundary({"read": "work_area"})

    assert boundary.check_path("a.txt", "write") is None
    refusal = boundary.check_path(area.path("../outside/z"), "write")
    assert refusal is not None and refusal.reason == "not_permitted"
    assert refusal.base == "work_area"


def test_a_workspace_working_directory_is_writable_except_agent_json(area: Area) -> None:
    boundary = area.boundary({"write": "work_area"}, workdir=area.workspace)

    assert boundary.check_path("MEMORY.md", "write") is None
    refusal = boundary.check_path("../agent.json", "write")
    assert refusal is not None and refusal.location == "agent_config"


def test_the_files_rules_come_from_stay_readable_but_unwritable(area: Area) -> None:
    profile = area.work / ".claude" / "agents" / "reviewer.md"
    missing = area.work / ".claude" / "settings.local.json"
    boundary = area.boundary(
        {"write": "work_area"}, agent_config=None, rule_files=(profile, missing)
    )
    ceiling = PermissionCeiling.from_ceiling_dict(boundary.to_ceiling_dict(source="parent"))
    # A Sub-Agent working elsewhere under that ceiling may not rewrite them either.
    child = PermissionBoundary.build(None, area.context(workdir=area.outside), ceilings=[ceiling])

    for checked in (boundary, child):
        assert checked.check_path(str(profile), "read") is None
        for path in (profile, missing):
            refusal = checked.check_path(str(path), "write")
            assert refusal is not None and refusal.location == "agent_config"
        assert checked.check_path(area.path("{work}/.claude"), "write", subtree=True) is not None
    assert boundary.check_path(area.path("{work}/.claude/notes.md"), "write") is None


def test_rules_whose_placeholder_has_no_value_are_dropped(area: Area) -> None:
    rules = {"write": "scratch", "allow": ["Edit({project}/**)"]}
    project = area.root / "project"

    without = area.boundary(rules, project_root=None)
    with_project = area.boundary(rules, project_root=project)

    assert without.layers[0].allow == ()
    assert without.check_path(str(project / "a.txt"), "write") is not None
    assert with_project.check_path(str(project / "a.txt"), "write") is None


def test_every_layer_must_permit_and_any_layer_can_deny(area: Area) -> None:
    parent = area.boundary({"read": "work_area", "deny": ["Read({workdir}/private)"]})
    ceiling = PermissionCeiling.from_ceiling_dict(parent.to_ceiling_dict(source="parent"))
    child = PermissionBoundary.build(
        normalize_permission_rules({"write": "scratch", "allow": ["Read({workdir}/private)"]}),
        area.context(workdir=area.outside),
        ceilings=[ceiling],
    )

    refusal = child.check_path(area.path("{outside}/notes.txt"), "read")
    assert refusal is not None and refusal.reason == "not_permitted"
    assert refusal.source == "parent"
    refusal = child.check_path(area.path("{work}/private/a.txt"), "read")
    assert refusal is not None and refusal.reason == "deny_rule" and refusal.source == "parent"
    assert child.check_path(area.path("{work}/a.txt"), "read") is None
    assert [layer.source for layer in child.layers] == ["agent", "parent"]


def test_ceilings_round_trip_as_json(area: Area) -> None:
    boundary = area.boundary(
        {
            "read": "work_area",
            "commands": "listed",
            "allow": ["Read(~/docs/**)", "Shell(git status)", "Web(example.com)"],
            "deny": ["Shell(git push *)"],
        }
    )
    stored = json.loads(json.dumps(boundary.to_ceiling_dict(source="cron")))

    ceiling = PermissionCeiling.from_ceiling_dict(stored)

    assert ceiling.to_ceiling_dict() == stored
    (layer,) = ceiling.layers
    assert layer.source == "cron"
    assert layer.commands == "listed"
    assert layer.shell_allow == ("git status",)
    assert layer.shell_deny == ("git push *",)
    inherited = PermissionBoundary.build(None, area.context(), ceilings=[ceiling])
    assert inherited.ceiling(source="parent") == ceiling


_LAYER: dict[str, object] = {
    "source": "parent",
    "read": "work_area",
    "write": "work_area",
    "commands": "any",
    "web": "any",
    "workdir": "/srv/work",
    "scratch": None,
    "allow": [],
    "deny": [],
}


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({}, id="no-layers-field"),
        pytest.param({"layers": []}, id="no-layer"),
        pytest.param({"layers": [_LAYER], "extra": 1}, id="extra-field"),
        pytest.param({"layers": [{**_LAYER, "colour": "red"}]}, id="unknown-layer-field"),
        pytest.param({"layers": [{**_LAYER, "source": "user"}]}, id="unknown-source"),
        pytest.param({"layers": [{**_LAYER, "read": None}]}, id="missing-base"),
        pytest.param({"layers": [{**_LAYER, "web": "some"}]}, id="unknown-base"),
        pytest.param({"layers": [{**_LAYER, "workdir": "work"}]}, id="relative-workdir"),
        pytest.param(
            {"layers": [{**_LAYER, "allow": ["Read({workdir}/x)"]}]}, id="placeholder-rule"
        ),
        pytest.param({"layers": [{**_LAYER, "deny": ["Edit(docs/**)"]}]}, id="relative-rule"),
        pytest.param({"layers": [{**_LAYER, "deny": ["Copy(x)"]}]}, id="malformed-rule"),
    ],
)
def test_malformed_ceilings_are_refused(stored: object) -> None:
    with pytest.raises(PermissionRulesError):
        PermissionCeiling.from_ceiling_dict(stored, platform="linux")


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        pytest.param("example.com", None, id="listed-host"),
        pytest.param("WWW.Example.com.", None, id="subdomain-of-listed-host"),
        pytest.param("bad.example.com", "deny_rule", id="denied-subdomain"),
        pytest.param("api.github.com", None, id="wildcard-subdomain"),
        pytest.param("github.com", "not_permitted", id="wildcard-excludes-the-domain"),
        pytest.param("10.0.0.5", None, id="ip-literal"),
        pytest.param("10.0.0.50", "not_permitted", id="other-ip"),
        # Hosts and rules compare IPv4 addresses in their dotted form.
        pytest.param("10.5", None, id="short-form-of-listed-ip"),
        pytest.param("167772165", None, id="number-form-of-listed-ip"),
        pytest.param("127.0.0.1", "deny_rule", id="ip-denied-in-short-form"),
        pytest.param("0x7f000001", "deny_rule", id="hex-form-of-denied-ip"),
        pytest.param("other.org", "not_permitted", id="unlisted-host"),
    ],
)
def test_host_decisions(area: Area, host: str, reason: str | None) -> None:
    boundary = area.boundary(
        {
            "web": "listed",
            "allow": ["Web(example.com)", "Web(*.github.com)", "Web(10.0.0.5)"],
            "deny": ["Web(bad.example.com)", "Web(127.1)"],
        }
    )

    refusal = boundary.check_host(host)

    assert (refusal.reason if refusal else None) == reason


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        pytest.param("http://127.0.0.1:22333/api/rpc", "protected_location", id="server-ipv4"),
        pytest.param("http://localhost:22333/", "protected_location", id="server-localhost"),
        pytest.param("http://[::1]:22333/", "protected_location", id="server-ipv6"),
        pytest.param("http://0.0.0.0:22333/", "protected_location", id="server-unspecified"),
        pytest.param("http://127.1:22333/", "protected_location", id="server-short-ipv4"),
        pytest.param("http://2130706433:22333/", "protected_location", id="server-ipv4-number"),
        pytest.param(
            f"http://{socket.gethostname()}:22333/", "protected_location", id="server-machine-name"
        ),
        pytest.param("http://127.0.0.1:5173/", None, id="other-local-port"),
        pytest.param("https://example.com/page", None, id="ordinary-url"),
        pytest.param("ftp://example.com/file", "unanalyzable", id="other-scheme"),
        pytest.param("http://example.com:99999/", "unanalyzable", id="invalid-port"),
    ],
)
def test_url_decisions(area: Area, url: str, reason: str | None) -> None:
    # Any restriction protects the vBot server; this one leaves the web open.
    refusal = area.boundary({"write": "scratch"}).check_url(url)

    assert (refusal.reason if refusal else None) == reason


def test_a_refusal_names_what_the_boundary_allows(area: Area) -> None:
    boundary = area.boundary(
        {"read": "work_area", "commands": "listed", "allow": ["Read(~/docs/**)", "Shell(make)"]}
    )

    refusal = boundary.check_path(area.path("{outside}/a.txt"), "read")

    assert refusal is not None
    assert refusal.allowed == (
        area.work.as_posix(),
        area.scratch.as_posix(),
        (area.home / "docs" / "**").as_posix(),
    )
    assert boundary.allowed_summary("command") == ("make",)
    assert boundary.allowed_summary("web") is None
    raised = PermissionDenied(refusal)
    assert raised.refusal is refusal
    assert refusal.target in str(raised)


@pytest.mark.parametrize(
    ("folder", "access", "rules", "expected"),
    [
        # Only deny rules and protected locations below a folder can refuse there.
        pytest.param("{work}", "read", _WORK_AREA_RULES, {"secrets"}, id="deny-below"),
        pytest.param(
            "{work}", "write", _WORK_AREA_RULES, {"secrets", "locked.txt"}, id="edit-deny-too"
        ),
        pytest.param("{work}/src", "read", _WORK_AREA_RULES, set(), id="nothing-below"),
        # A wildcard rule above the folder can still match at any depth below it.
        pytest.param(
            "{work}/src", "read", {"deny": ["Read({workdir}/**/.env)"]}, {"**/.env"}, id="wildcard"
        ),
        # Under any restriction, the data directory and the Agent's own configuration
        # are protected below a root.
        pytest.param(
            "{root}",
            "read",
            {"web": "listed"},
            {"data", "data/agents/a1/agent.json"},
            id="protected-locations",
        ),
        pytest.param(
            "{root}",
            "write",
            {"web": "listed"},
            {"data", "data/agents/a1/agent.json", "install"},
            id="installs",
        ),
    ],
)
def test_refusable_below_names_what_may_be_refused_under_a_folder(
    area: Area, folder: str, access: PathAccess, rules: object, expected: set[str]
) -> None:
    boundary = area.boundary(rules)
    path = area.path(folder.replace("{root}", str(area.root)))

    found = set(boundary.refusable_below(path, access))

    assert found == {key.casefold() if WINDOWS else key for key in expected}
    assert PermissionBoundary.UNRESTRICTED.refusable_below(path, access) == ()


def test_readable_entries_leave_out_refused_names_and_links_leading_out(area: Area) -> None:
    (area.work / "notes.txt").write_text("x", encoding="utf-8")
    link_directory(area.work / "elsewhere", area.outside)
    denying = area.boundary({"read": "work_area", "deny": ["Read({workdir}/secrets/**)"]})
    plain = area.boundary({"read": "work_area"})

    def names(boundary: PermissionBoundary) -> set[str]:
        with os.scandir(area.work) as listing:
            return {entry.name for entry in boundary.readable_entries(area.work, listing)}

    assert names(denying) == {"notes.txt"}
    # Without a rule below the folder, only the link needs a check.
    assert names(plain) == {"notes.txt", "secrets"}
    assert names(PermissionBoundary.UNRESTRICTED) == {"notes.txt", "secrets", "elsewhere"}
