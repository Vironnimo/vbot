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
        workdir=work,
        home=work / "home",
        data_dir=work.parent / "data",
        loopback_port=PORT,
        own_hosts=("192.168.1.20",),
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
        pytest.param(
            RESTRICTED,
            None,
            (f"curl -X POST 192.168.1.20:{PORT}/api/rpc",),
            "protected_location",
            id="lan-address-port-word",
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


@pytest.mark.asyncio
async def test_hard_command_refusal_precedes_missing_access(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    analyzer = FakeAnalyzer(
        plan(
            "cat file",
            reads=(outside,),
            unresolved=(UnresolvedElement("command", "eval", "it is computed"),),
        )
    )
    checker = fake_checker(analyzer)
    restricted = boundary(tmp_path, {"read": "work_area"})
    plan_result, decision = await checker.prepare_line(
        restricted, "cat file; eval $x", shell="bash", cwd=tmp_path, env={}, home=tmp_path
    )
    assert plan_result is not None
    assert {item.reason for item in decision.refusals} == {"not_permitted", "unanalyzable"}
    refusal = await checker.check_line(
        restricted, "cat file; eval $x", shell="bash", cwd=tmp_path, env={}, home=tmp_path
    )
    assert refusal is not None and refusal.reason == "unanalyzable"


@pytest.mark.parametrize(
    ("command", "cwd_in_checkout", "python_path", "reason"),
    [
        pytest.param(
            "python {root}/cli/main.py agent list",
            False,
            None,
            "protected_location",
            id="cli-script",
        ),
        pytest.param(
            "python cli/main.py agent list",
            True,
            None,
            "protected_location",
            id="relative-cli-script",
        ),
        pytest.param(
            "python -m cli.main agent list", True, None, "protected_location", id="cli-module"
        ),
        pytest.param("python -m cli.main agent list", False, None, None, id="other-cli-module"),
        pytest.param("python tools/build.py", True, None, None, id="other-script"),
        pytest.param(
            '"{root}/.venv/Scripts/python.exe" -m cli agent list',
            False,
            None,
            "protected_location",
            id="checkout-interpreter",
        ),
        pytest.param(
            'env "{root}/.venv/Scripts/python.exe" -mserver.main',
            False,
            None,
            "protected_location",
            id="wrapped-checkout-interpreter",
        ),
        pytest.param(
            "python -m cli agent list",
            False,
            "{root}",
            "protected_location",
            id="pythonpath-checkout",
        ),
        pytest.param(
            "python -m cli agent list",
            False,
            "../vbot",
            "protected_location",
            id="relative-pythonpath",
        ),
        pytest.param("python -m cli agent list", False, "{other}", None, id="unrelated-pythonpath"),
        pytest.param("python -m compileall .", False, "{root}", None, id="unrelated-module"),
        pytest.param(
            "PYTHONPATH={root} python -m cli agent list",
            False,
            None,
            "unanalyzable",
            id="changed-pythonpath",
        ),
        pytest.param(
            "env PYTHONPATH={root} python -m cli agent list",
            False,
            None,
            "unanalyzable",
            id="wrapped-changed-pythonpath",
        ),
    ],
)
@pytest.mark.asyncio
async def test_vbots_own_cli_cannot_be_started(
    real_analyzer: CommandAnalyzer,
    tmp_path: Path,
    command: str,
    cwd_in_checkout: bool,
    python_path: str | None,
    reason: str | None,
) -> None:
    root = tmp_path / "vbot"
    work = root if cwd_in_checkout else tmp_path / "work"
    command = command.format(root=root.as_posix())
    env = (
        {}
        if python_path is None
        else {
            "PYTHONPATH": python_path.format(
                root=root.as_posix(), other=(tmp_path / "other").as_posix()
            )
        }
    )
    checker = CommandChecker(lambda: real_analyzer, vbot_root=root)

    refusal = await checker.check_line(
        boundary(work, RESTRICTED), command, shell="bash", cwd=work, env=env, home=tmp_path
    )

    assert (refusal.reason if refusal else None) == reason
    if reason == "protected_location":
        assert refusal is not None and refusal.location == "vbot_cli"


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
            "bash",
            "curl --json '{\"message\":\"hello\"}' --data-raw 'hello world' https://ok.example",
            None,
            id="curl-inline-payloads-stay-allowed",
        ),
        pytest.param(
            "bash",
            "curl --json @../outside.json https://ok.example",
            "not_permitted",
            id="curl-json-file-outside-is-refused",
        ),
        *[
            pytest.param("bash", line, reason, id=id)
            for line, reason, id in (
                (
                    f"curl --resolve ok.example:{PORT}:127.0.0.1 http://ok.example:{PORT}/api",
                    "protected_location",
                    "curl-dns-override-protects-server-port",
                ),
                (
                    f"curl --connect-to ok.example:{PORT}:127.0.0.1: http://ok.example:{PORT}/api",
                    "protected_location",
                    "curl-connect-override-inherits-server-port",
                ),
                (
                    f"curl --connect-to ::127.0.0.1: http://ok.example:{PORT}/api",
                    "protected_location",
                    "curl-wildcard-override-inherits-url-port",
                ),
                (
                    f"curl --connect-to :::{PORT} http://localhost:8000/api",
                    "protected_location",
                    "curl-port-override-inherits-url-host",
                ),
                (
                    "curl --resolve ok.example:8000:127.0.0.1 http://ok.example:8000/api",
                    None,
                    "curl-dns-override-other-port-stays-allowed",
                ),
            )
        ],
        pytest.param("bash", "../tools/viewer.exe --version", None, id="native-outside"),
        pytest.param("bash", '"{native}" --version', None, id="native-absolute-outside"),
        pytest.param(
            "bash", "./secrets/viewer.exe --version", "deny_rule", id="native-read-denied"
        ),
        pytest.param(
            "bash", "../data/viewer.exe --version", "protected_location", id="native-protected"
        ),
        pytest.param("bash", "../tools/run.sh", "not_permitted", id="script-outside"),
        *[
            pytest.param(
                "powershell",
                line,
                reason,
                id=id,
                marks=pytest.mark.skipif(
                    sys.platform != "win32" or _PWSH is None, reason="PowerShell's own parser"
                ),
            )
            for line, reason, id in (
                (
                    "Start-BitsTransfer -Source https://ok.example/secrets",
                    "deny_rule",
                    "bits-implicit-destination-checks-children",
                ),
                (
                    "Start-BitsTransfer -Source https://ok.example/secrets -Destination .",
                    "deny_rule",
                    "bits-explicit-directory-checks-children",
                ),
                (
                    "Start-BitsTransfer -Source https://ok.example/file -Destination out/file",
                    None,
                    "bits-destination-outside-denied-subtree-stays-allowed",
                ),
                ("pytest -q | Select-String passed", None, "native-output-is-text"),
                ("pytest -q | Remove-Item", "unanalyzable", "native-output-is-not-a-file-list"),
                (
                    "$s=Get-Content a.txt -Raw; $s=$s.Replace('a','b'); Set-Content a.txt $s",
                    None,
                    "string-replacement-inside",
                ),
                (
                    "(Get-Item a.txt).Replace('../outside.txt', 'backup.txt')",
                    "not_permitted",
                    "file-replacement-outside",
                ),
                ("gh api /repos/owner/repo/pulls", None, "route-is-not-a-host-read"),
                ("rg '/api/rpc' src", None, "search-pattern-is-not-a-file"),
                (
                    "$p='src/a.txt'; if (Test-Path $p) { Get-Content $p }",
                    None,
                    "conditional-constant-inside",
                ),
                (
                    "$p='../outside.txt'; if (Test-Path $p) { Get-Content $p }",
                    "not_permitted",
                    "conditional-constant-outside",
                ),
                (
                    "$p=Join-Path .. outside.txt; Set-Content $p x",
                    "not_permitted",
                    "joined-path-outside",
                ),
                (
                    "$c='Remove-Item'; & $c ../outside.txt",
                    "not_permitted",
                    "constant-cmdlet-outside",
                ),
                (
                    "foreach ($p in 'src/a','../outside.txt') { Set-Content $p x }",
                    "not_permitted",
                    "literal-loop-outside",
                ),
                (
                    "Get-ChildItem src | ForEach-Object { Get-Content $_.FullName }",
                    None,
                    "listing-loop-inside",
                ),
                (
                    "Get-ChildItem secrets | ForEach-Object { Get-Content $_.FullName }",
                    "deny_rule",
                    "listing-loop-denied",
                ),
            )
        ],
        # curl.exe on Windows requests fass.at for this name, other programs xn--fa-hia.at.
        pytest.param("bash", "curl -s https://faß.at/", "deny_rule", id="host-a-program-reads"),
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
    line = line.replace("{native}", (tmp_path / "tools" / "viewer.exe").as_posix())
    checker = CommandChecker(lambda: real_analyzer)
    rules: dict[str, object] = {
        "read": "work_area",
        "write": "work_area",
        "deny": ["Read({workdir}/secrets/**)", "Web(fass.at)"],
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


@pytest.mark.parametrize(
    ("line", "deny", "reason"),
    [
        pytest.param("git rm secrets/key.pem", True, "deny_rule", id="rm-denied"),
        pytest.param("git checkout -- secrets", True, "deny_rule", id="checkout-folder-denied"),
        pytest.param("git mv secrets/key.pem k", True, "deny_rule", id="mv-denied"),
        pytest.param(
            "git checkout -- .claude/settings.json",
            False,
            "protected_location",
            id="checkout-rule-file",
        ),
        pytest.param("git restore .", False, None, id="restore-folder-may-update-rule-files"),
        pytest.param("git apply fix.patch", False, None, id="apply-may-update-rule-files"),
        pytest.param("git checkout -- .", True, None, id="checkout-folder-with-a-deny-below"),
        pytest.param("git reset --hard", True, None, id="reset-hard-with-a-deny-below"),
        pytest.param("git -C sub stash pop", True, None, id="stash-from-a-subfolder"),
        pytest.param("git pull", True, None, id="pull-with-a-deny-below"),
        pytest.param("git switch main", False, None, id="switch-may-update-rule-files"),
        pytest.param("git clean -fdx", False, "protected_location", id="clean-folder"),
        pytest.param("git stash -u", True, "protected_location", id="stash-untracked"),
        pytest.param("git rm -r .", True, "protected_location", id="rm-folder"),
        pytest.param("git restore '*.pem'", True, "protected_location", id="wildcard-pathspec"),
        pytest.param("git restore --staged secrets/key.pem", True, None, id="index-only"),
        pytest.param("git diff -- secrets", "read", "deny_rule", id="diff-denied"),
        pytest.param("git add secrets", "read", "deny_rule", id="add-reads"),
        pytest.param("git add .", "read", None, id="add-folder-like-add-all"),
        pytest.param("git add -A", "read", None, id="add-all"),
        pytest.param("git diff .", "read", None, id="diff-folder-like-pathless-diff"),
        pytest.param("git grep foo", "read", "deny_rule", id="grep-prints-contents"),
        pytest.param("git -C sub cat-file -p :secrets/k", "read", "deny_rule", id="index-path"),
        pytest.param("git ls-files secrets", "read", "deny_rule", id="ls-files-denied"),
        pytest.param("git diff --stat", "read", None, id="pathless-diff"),
        pytest.param("git show HEAD:secrets/k", "read", None, id="history-unchecked"),
    ],
)
@pytest.mark.asyncio
async def test_git_reads_and_changes_what_its_paths_or_work_tree_reach(
    real_analyzer: CommandAnalyzer,
    tmp_path: Path,
    line: str,
    deny: bool | str,
    reason: str | None,
) -> None:
    # deny: an Edit deny of secrets, or a Read deny with "read".
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    (work / "sub").mkdir()
    context = BoundaryContext(
        workdir=work,
        home=tmp_path / "home",
        data_dir=tmp_path / "data",
        rule_files=(work / ".claude" / "settings.json", work / ".claude" / "settings.local.json"),
    )
    rules: dict[str, object] = {"read": "work_area", "write": "work_area"}
    if deny:
        rules["deny"] = [
            "Read({workdir}/secrets/**)" if deny == "read" else "Edit({workdir}/secrets/**)"
        ]
    checker = CommandChecker(lambda: real_analyzer)

    refusal = await checker.check_line(
        PermissionBoundary.build(normalize_permission_rules(rules), context),
        line,
        shell="bash",
        cwd=work,
        env={},
        home=tmp_path,
    )

    assert (refusal.reason if refusal else None) == reason


@pytest.mark.parametrize("ancestor_repository", [True, False])
@pytest.mark.parametrize("line", ["git reset --hard", "git init; git checkout -b task"])
@pytest.mark.parametrize(
    "shell",
    [
        "bash",
        pytest.param(
            "powershell",
            marks=pytest.mark.skipif(_PWSH is None, reason="PowerShell's own parser needs pwsh"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_unresolved_work_tree_root_explains_the_preflight_limit(
    real_analyzer: CommandAnalyzer,
    tmp_path: Path,
    ancestor_repository: bool,
    line: str,
    shell: str,
) -> None:
    if ancestor_repository:
        (tmp_path / ".git").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    checker = CommandChecker(lambda: real_analyzer)

    refusal = await checker.check_line(
        boundary(work, {"read": "work_area", "write": "work_area"}),
        line,
        shell=shell,  # type: ignore[arg-type]
        cwd=work,
        env={},
        home=tmp_path,
    )

    # No command ran. An unreadable ancestor is not evidence that Git would
    # change it; the same preflight cannot see a repository created later.
    assert refusal is not None and refusal.reason == "unanalyzable"
    assert refusal.target == line
    assert refusal.detail is not None and work.as_posix() in refusal.detail
    assert refusal.hint is not None and "separate call" in refusal.hint
    assert not refusal.retryable

    # Once the requested repository exists, the remaining command is checked
    # against its actual root and succeeds inside the permitted folder.
    (work / ".git").mkdir()
    assert (
        await checker.check_line(
            boundary(work, {"read": "work_area", "write": "work_area"}),
            "git checkout -b task",
            shell=shell,  # type: ignore[arg-type]
            cwd=work,
            env={},
            home=tmp_path,
        )
        is None
    )
