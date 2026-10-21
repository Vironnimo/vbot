"""Contract tests of ``CommandChecker``: what a restricted command line may run, and its analyzer.

Most cases decide a fixed plan from ``FakeAnalyzer``; the Tools' own tests show
where each refusal reaches the Agent. A few cases run the real analysis (bash in
process everywhere, PowerShell where its helper can start) to show the checker
and the analysis agree on the plan's shape.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from core.permissions import (
    BoundaryContext,
    CommandChecker,
    PermissionBoundary,
    PermissionCeiling,
    normalize_permission_rules,
)
from core.permissions.commands.commands import CommandAnalyzer, UnresolvedElement
from tests.core.permissions.command_check_support import (
    UNAVAILABLE,
    FakeAnalyzer,
    fake_checker,
    plan,
)

PORT = 22333


# Restricts something unrelated, so vBot's own CLI and server are protected.
RESTRICTED: dict[str, object] = {"deny": ["Shell(never-run-this *)"]}


def boundary(
    work: Path, rules: dict[str, object], *, ceiling: dict[str, object] | None = None
) -> PermissionBoundary:
    context = BoundaryContext(
        workdir=work, home=work / "home", data_dir=work.parent / "data", loopback_port=PORT
    )
    ceilings: tuple[PermissionCeiling, ...] = ()
    if ceiling is not None:
        parent = PermissionBoundary.build(normalize_permission_rules(ceiling), context)
        stored = parent.ceiling(source="parent")
        assert stored is not None
        ceilings = (PermissionCeiling.from_ceiling_dict(stored.to_ceiling_dict()),)
    return PermissionBoundary.build(normalize_permission_rules(rules), context, ceilings=ceilings)


@pytest.mark.parametrize(
    ("rules", "ceiling", "commands", "reason"),
    [
        pytest.param(
            {"commands": "listed", "allow": ["Shell(git *)"]},
            {"commands": "listed", "allow": ["Shell(git status)"]},
            ("git log",),
            "not_permitted",
            id="every-listing-layer-must-allow",
        ),
        pytest.param(
            {"commands": "listed", "allow": ["Shell(git *)"]},
            {"commands": "listed", "allow": ["Shell(git status)"]},
            ("git status",),
            None,
            id="allowed-by-every-layer",
        ),
        pytest.param(
            {},
            {"deny": ["Shell(npm publish *)"]},
            ("npm publish",),
            "deny_rule",
            id="inherited-deny",
        ),
        pytest.param(
            RESTRICTED, None, ("vbot.ps1 agent list",), "protected_location", id="vbot-script"
        ),
        pytest.param(
            RESTRICTED,
            None,
            (f"curl ws://[::1]:{PORT}/live",),
            "protected_location",
            id="host-port-word",
        ),
        pytest.param(RESTRICTED, None, (f"curl localhost:{PORT + 1}",), None, id="other-port"),
    ],
)
@pytest.mark.asyncio
async def test_command_decisions(
    tmp_path: Path,
    rules: dict[str, object],
    ceiling: dict[str, object] | None,
    commands: tuple[str, ...],
    reason: str | None,
) -> None:
    checker = fake_checker(FakeAnalyzer(plan(*commands)))

    refusal = await checker.check_line(
        boundary(tmp_path, rules, ceiling=ceiling),
        " ; ".join(commands),
        shell="bash",
        cwd=tmp_path,
        env={},
        home=tmp_path,
    )

    assert (refusal.reason if refusal else None) == reason


@pytest.mark.parametrize(
    ("command", "cwd_in_checkout", "refused"),
    [
        pytest.param("python {root}/cli/main.py agent list", False, True, id="cli-script"),
        pytest.param("python cli/main.py agent list", True, True, id="relative-cli-script"),
        pytest.param("python -m cli.main agent list", True, True, id="cli-module"),
        pytest.param("python -m cli.main agent list", False, False, id="other-cli-module"),
        pytest.param("python tools/build.py", True, False, id="other-script"),
    ],
)
@pytest.mark.asyncio
async def test_vbots_own_cli_cannot_be_started(
    tmp_path: Path, command: str, cwd_in_checkout: bool, refused: bool
) -> None:
    root = tmp_path / "vbot"
    work = root if cwd_in_checkout else tmp_path / "work"
    analyzer = FakeAnalyzer(plan(command.replace("{root}", root.as_posix())))
    checker = fake_checker(analyzer, vbot_root=root)

    refusal = await checker.check_line(
        boundary(work, RESTRICTED), command, shell="bash", cwd=work, env={}, home=tmp_path
    )

    assert (refusal.location if refusal else None) == ("vbot_cli" if refused else None)


@pytest.mark.asyncio
async def test_the_analyzer_starts_on_the_first_restricted_check_and_ends_with_the_checker(
    tmp_path: Path,
) -> None:
    analyzer = FakeAnalyzer(plan("ls"))
    created: list[FakeAnalyzer] = []

    def factory() -> FakeAnalyzer:
        created.append(analyzer)
        return analyzer

    checker = CommandChecker(factory)  # type: ignore[arg-type]
    restricted = boundary(tmp_path, RESTRICTED)

    unrestricted = await checker.check_line(
        PermissionBoundary.UNRESTRICTED, "ls", shell="bash", cwd=tmp_path, env={}, home=tmp_path
    )
    assert unrestricted is None and created == []
    for _ in range(2):
        assert (
            await checker.check_line(
                restricted, "ls", shell="bash", cwd=tmp_path, env={}, home=tmp_path
            )
            is None
        )
    assert created == [analyzer]

    analyzer.result = UNAVAILABLE
    failed = await checker.check_line(
        restricted, "ls", shell="bash", cwd=tmp_path, env={}, home=tmp_path
    )
    assert failed is not None and failed.reason == "unanalyzable" and failed.retryable
    assert "If the task needs this access" not in failed.message("bash")

    await checker.aclose()
    assert analyzer.closed
    closed = await checker.check_line(
        restricted, "ls", shell="bash", cwd=tmp_path, env={}, home=tmp_path
    )
    assert closed is not None and closed.reason == "unanalyzable"


@pytest.mark.parametrize("workdir_parameter", [True, False])
@pytest.mark.asyncio
async def test_an_unknown_folder_suggests_workdir_only_where_the_tool_takes_it(
    tmp_path: Path, workdir_parameter: bool
) -> None:
    unknown = UnresolvedElement("cwd", "$d", "the folder is computed when it runs")
    checker = fake_checker(FakeAnalyzer(plan("cd", unresolved=[unknown])))

    refusal = await checker.check_line(
        boundary(tmp_path, {"read": "work_area", "write": "work_area"}),
        "cd $d",
        shell="bash",
        cwd=tmp_path,
        env={},
        home=tmp_path,
        workdir_parameter=workdir_parameter,
    )

    assert refusal is not None and not refusal.retryable
    assert refusal.hint == (
        "Change into folders by their literal path, or pass the folder as workdir."
        if workdir_parameter
        else "Change into folders by their literal path."
    )


@pytest.mark.asyncio
async def test_a_terminal_program_is_checked_as_one_command(tmp_path: Path) -> None:
    analyzer = FakeAnalyzer(plan("ssh prod"))
    checker = fake_checker(analyzer)

    refusal = await checker.check_argv(
        boundary(tmp_path, {"deny": ["Shell(ssh *)"]}),
        ["ssh", "prod"],
        cwd=tmp_path,
        env={},
        home=tmp_path,
    )

    assert refusal is not None and refusal.rule == "Shell(ssh *)"
    assert analyzer.argvs == [(("ssh", "prod"), tmp_path)]


_PWSH = shutil.which("pwsh")


@pytest_asyncio.fixture(scope="module")
async def real_analyzer() -> AsyncIterator[CommandAnalyzer]:
    """One analyzer, and so one PowerShell helper, for this module's real-analysis cases."""
    analyzer = CommandAnalyzer(powershell_executable=Path(_PWSH) if _PWSH else None)
    yield analyzer
    await analyzer.aclose()


@pytest.mark.parametrize(
    ("shell", "line", "reason"),
    [
        pytest.param("bash", "cat ../outside.txt", "not_permitted", id="bash-read-outside"),
        pytest.param("bash", "rm -r build && git status", None, id="bash-inside"),
        # A program without a spec: each word, and an option's value, is checked
        # against deny rules and protected locations, and passes otherwise.
        pytest.param("bash", "awk 1 --file=secrets/key.txt", "deny_rule", id="mention-denied"),
        pytest.param("bash", "awk '{print}' notes --out=x", None, id="mention-allowed"),
        pytest.param("bash", "echo secrets/key.txt", None, id="no-path-program"),
        pytest.param(
            "powershell",
            "Set-Content -Path ..\\outside.txt -Value x",
            "not_permitted",
            id="powershell-write-outside",
            marks=pytest.mark.skipif(
                sys.platform != "win32" or _PWSH is None, reason="PowerShell's own parser"
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_real_analysis_reaches_the_same_decisions(
    real_analyzer: CommandAnalyzer, tmp_path: Path, shell: str, line: str, reason: str | None
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    checker = CommandChecker(lambda: real_analyzer)
    rules: dict[str, object] = {
        "read": "work_area",
        "write": "work_area",
        "deny": ["Read({workdir}/secrets/**)"],
    }
    refusal = await checker.check_line(
        boundary(work, rules),
        line,
        shell=shell,  # type: ignore[arg-type]
        cwd=work,
        env={},
        home=tmp_path,
    )

    assert (refusal.reason if refusal else None) == reason
