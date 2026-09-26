import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "quality.py"


def _load_quality_module():
    spec = importlib.util.spec_from_file_location("quality", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_filter_pytest_failure_output_removes_pass_noise():
    module = _load_quality_module()
    output = "\n".join(
        [
            "============================= test session starts =============================",
            "platform win32 -- Python 3.14.4, pytest-9.0.3",
            "cachedir: .pytest_cache",
            "rootdir: C:/Development/projects/vBot",
            "plugins: xdist",
            "tests/example/test_demo.py::test_ok",
            "[gw0] [ 50%] tests/example/test_demo.py::test_ok",
            "tests/example/test_demo.py::test_ok PASSED                                 [ 50%]",
            "tests/example/test_demo.py::test_bad FAILED                                [100%]",
            "",
            "================================== FAILURES ===================================",
            "_________________________________ test_bad __________________________________",
            "tests/example/test_demo.py:10: in test_bad",
            "    assert 1 == 2",
            "E   assert 1 == 2",
            "",
            "=========================== short test summary info ===========================",
            "FAILED tests/example/test_demo.py::test_bad - assert 1 == 2",
            "========================= 1 failed, 1 passed in 0.12s =========================",
        ]
    )

    filtered = module.filter_pytest_failure_output(output)

    assert "test session starts" not in filtered
    assert "platform win32" not in filtered
    assert "tests/example/test_demo.py::test_ok\n" not in filtered
    assert "[gw0] [ 50%] tests/example/test_demo.py::test_ok" not in filtered
    assert "PASSED" not in filtered
    assert "tests/example/test_demo.py::test_bad FAILED" in filtered
    assert "FAILED tests/example/test_demo.py::test_bad - assert 1 == 2" in filtered
    assert "1 failed, 1 passed in 0.12s" in filtered


def test_filter_pytest_failure_output_removes_bare_nodeid_progress_lines():
    module = _load_quality_module()
    output = "\n".join(
        [
            "tests/example/test_demo.py::test_ok",
            "[gw1] [ 50%] tests/example/test_demo.py::test_ok",
            "tests/example/test_demo.py::test_bad FAILED                                [100%]",
            "",
            "================================== FAILURES ===================================",
            "_________________________________ test_bad __________________________________",
            "tests/example/test_demo.py:10: in test_bad",
            "    assert 1 == 2",
            "E   assert 1 == 2",
            "",
            "=========================== short test summary info ===========================",
            "FAILED tests/example/test_demo.py::test_bad - assert 1 == 2",
            "========================= 1 failed, 1 passed in 0.12s =========================",
        ]
    )

    filtered = module.filter_pytest_failure_output(output)

    assert "tests/example/test_demo.py::test_ok\n" not in filtered
    assert "[gw1] [ 50%] tests/example/test_demo.py::test_ok" not in filtered
    assert "tests/example/test_demo.py::test_bad FAILED" in filtered
    assert "FAILED tests/example/test_demo.py::test_bad - assert 1 == 2" in filtered
    assert "1 failed, 1 passed in 0.12s" in filtered


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/example/test_demo.py::test_text[ID3 tags]",
        "tests/example/test_demo.py::TestExample::test_text[with spaces]",
        r"tests/example/test_demo.py::test_text[```\nSession naming audit\n```]",
        "tests/example/test_demo.py::test_text[expected FAILED result]",
        "[gw1] [ 50%] tests/example/test_demo.py::test_text[quoted [nested] value]",
    ],
)
def test_filter_pytest_removes_parameterized_progress_but_keeps_failures(nodeid):
    module = _load_quality_module()
    failure = f"{nodeid} FAILED [100%]"
    report = "\n".join(
        [
            "================ FAILURES ================",
            "E   AssertionError: expected value",
            "FAILED tests/example/test_demo.py::test_bad - AssertionError",
        ]
    )

    filtered = module.filter_pytest_failure_output(f"{nodeid}\n{failure}\n{report}")

    assert filtered == f"{failure}\n{report}"


def test_filter_pytest_preserves_progress_like_text_in_diagnostics():
    module = _load_quality_module()
    report = "\n".join(
        [
            "================ FAILURES ================",
            "E   AssertionError: expected PASSED status",
            "---------------- Captured stdout call ----------------",
            "tests/example/test_demo.py::test_text[diagnostic value]",
            "command PASSED but produced the wrong result",
            "platform details from the tested program",
            "================ short test summary info ================",
            "FAILED tests/example/test_demo.py::test_bad - AssertionError",
        ]
    )

    filtered = module.filter_pytest_failure_output(
        "tests/example/test_demo.py::test_ok[successful value]\n" + report
    )

    assert filtered == report


def test_extract_pytest_profile_output_returns_only_duration_section():
    module = _load_quality_module()
    output = "\n".join(
        [
            "============================= slowest 25 durations =============================",
            "1.25s call     tests/example/test_demo.py::test_slow",
            "0.40s setup    tests/example/test_demo.py::test_setup",
            "============================== 2 passed in 1.70s ==============================",
        ]
    )

    profile = module.extract_pytest_profile_output(output)

    assert "slowest 25 durations" in profile
    assert "test_slow" in profile
    assert "test_setup" in profile
    assert "2 passed" not in profile


def test_main_runs_pytest_verbose(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "core/runtime/"])

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        if cmd[2] == "pytest":
            return module.subprocess.CompletedProcess(
                cmd, 0, stdout="1 passed in 0.01s\n", stderr=""
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    capsys.readouterr()
    pytest_command = next(cmd for cmd in commands if cmd[2] == "pytest")
    assert pytest_command[:5] == [module.sys.executable, "-m", "pytest", "-v", "--tb=short"]


def test_profile_mode_requests_and_prints_slowest_pytest_durations(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []
    pytest_output = "\n".join(
        [
            "============================= slowest 25 durations =============================",
            "1.25s call     tests/example/test_demo.py::test_slow",
            "============================== 1 passed in 1.25s ==============================",
        ]
    )
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["quality.py", "--profile", "tests/scripts/test_quality.py"],
    )

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        stdout = f"{pytest_output}\n" if cmd[2] == "pytest" else ""
        return module.subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    captured = capsys.readouterr()
    pytest_command = next(cmd for cmd in commands if cmd[2] == "pytest")
    assert f"--durations={module.PYTEST_PROFILE_COUNT}" in pytest_command
    assert "--- pytest profile ---" in captured.out
    assert "test_slow" in captured.out
    assert "1 passed in 1.25s" not in captured.out


def test_help_exits_successfully_without_running_tools(monkeypatch):
    module = _load_quality_module()
    monkeypatch.setattr(module.sys, "argv", ["quality.py", "--help"])

    def fail_run(*args, **kwargs):
        raise AssertionError("help must not run quality tools")

    monkeypatch.setattr(module.subprocess, "run", fail_run)

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 0


def test_check_mode_validates_without_fix_commands(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(module.sys, "argv", ["quality.py", "--check", "core/runtime/"])

    def fail_snapshot(*args, **kwargs):
        raise AssertionError("check mode must not snapshot for source mutations")

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        returncode = 1 if cmd[2:5] == ["ruff", "format", "--check"] else 0
        stdout = "1 passed in 0.01s\n" if cmd[2] == "pytest" else ""
        return module.subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(module, "snapshot_target_files", fail_snapshot)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    captured = capsys.readouterr()
    assert "ruff format   .... FAIL" in captured.out
    assert [module.sys.executable, "-m", "ruff", "format", "--check", "core/runtime"] in commands
    assert not any("--fix" in command for command in commands)


def test_main_filters_pytest_failure_output(monkeypatch, capsys):
    module = _load_quality_module()
    pytest_output = "\n".join(
        [
            "============================= test session starts =============================",
            "platform win32 -- Python 3.14.4, pytest-9.0.3",
            "tests/example/test_demo.py::test_ok",
            "[gw0] [ 50%] tests/example/test_demo.py::test_ok",
            "tests/example/test_demo.py::test_ok PASSED                                 [ 50%]",
            "tests/example/test_demo.py::test_bad FAILED                                [100%]",
            "",
            "================================== FAILURES ===================================",
            "_________________________________ test_bad __________________________________",
            "tests/example/test_demo.py:10: in test_bad",
            "    assert 1 == 2",
            "E   assert 1 == 2",
            "",
            "=========================== short test summary info ===========================",
            "FAILED tests/example/test_demo.py::test_bad - assert 1 == 2",
            "========================= 1 failed, 1 passed in 0.12s =========================",
        ]
    )

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "tests/scripts/test_quality.py"])

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[2] == "pytest":
            return module.subprocess.CompletedProcess(cmd, 1, stdout=pytest_output, stderr="")
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    captured = capsys.readouterr()
    assert "--- pytest ---" in captured.out
    assert "[gw0] [ 50%] tests/example/test_demo.py::test_ok" not in captured.out
    assert "tests/example/test_demo.py::test_ok\n" not in captured.out
    assert "tests/example/test_demo.py::test_ok PASSED" not in captured.out
    assert "FAILED tests/example/test_demo.py::test_bad - assert 1 == 2" in captured.out


def test_translate_maps_existing_mirror_file():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(["core/prompts/prompts.py"])

    assert test_paths == [
        "tests/core/prompts/test_prompts_assembly.py",
        "tests/core/prompts/test_prompts_edit_facade.py",
        "tests/core/prompts/test_prompts_layouts_overrides.py",
        "tests/core/prompts/test_prompts_skill_catalog.py",
        "tests/core/prompts/test_prompts_tools_skills_extensions.py",
    ]
    assert notes == []


def test_translate_falls_back_to_mirror_directory_with_note():
    module = _load_quality_module()

    # A package __init__.py has no dedicated mirror test by nature, so it always
    # exercises the fallback: no owned test_*.py, run the mirror directory + note.
    test_paths, notes = module.translate_to_test_paths(["core/settings/__init__.py"])

    assert test_paths == ["tests/core/settings"]
    assert len(notes) == 1
    assert "core/settings/__init__.py" in notes[0]
    assert "tests/core/settings" in notes[0]


def test_translate_maps_non_core_packages():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(
        ["server/app.py", "scripts/quality.py", "cli"]
    )

    assert "tests/server/test_app.py" in test_paths
    assert "tests/scripts/test_quality.py" in test_paths
    assert "tests/cli" in test_paths
    assert notes == []


def test_translate_notes_unmirrored_package_instead_of_pytest_arg():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(["webui/src/App.svelte"])

    assert test_paths == []
    assert len(notes) == 1
    assert "webui/src/App.svelte" in notes[0]


def test_translate_includes_split_sibling_test_files():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(["core/providers/openai_compatible.py"])

    # The behavior suites and oauth sibling have no own source files, so they
    # all belong to the adapter even without an exact mirror collector.
    assert "tests/core/providers/test_openai_compatible_requests.py" in test_paths
    assert "tests/core/providers/test_openai_compatible_oauth.py" in test_paths
    assert notes == []


def test_translate_does_not_claim_more_specific_siblings():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(["core/providers/openai.py"])

    # openai.py must not pull in openai_compatible's mirrors: the longer source
    # stem owns them.
    assert "tests/core/providers/test_openai.py" in test_paths
    assert not any("test_openai_compatible" in path for path in test_paths)
    assert notes == []


def test_translate_normalizes_hyphenated_source_stem():
    module = _load_quality_module()

    frontend_paths, frontend_notes = module.translate_to_test_paths(["scripts/quality-frontend.py"])
    quality_paths, quality_notes = module.translate_to_test_paths(["scripts/quality.py"])

    # quality-frontend.py mirrors to the underscore module test_quality_frontend.py,
    # and quality.py must not swallow it via the shared "quality" prefix.
    assert frontend_paths == ["tests/scripts/test_quality_frontend.py"]
    assert frontend_notes == []
    assert quality_paths == ["tests/scripts/test_quality.py"]
    assert quality_notes == []


def test_owning_source_stem_prefers_longest_prefix():
    module = _load_quality_module()

    source_stems = ["openai_compatible", "openai"]  # longest first

    assert (
        module._owning_source_stem("openai_compatible_oauth", source_stems) == "openai_compatible"
    )
    assert module._owning_source_stem("openai", source_stems) == "openai"
    assert module._owning_source_stem("unrelated", source_stems) is None


def test_main_rejects_unknown_input_path(monkeypatch, capsys):
    module = _load_quality_module()
    monkeypatch.setattr(module.sys, "argv", ["quality.py", "core/does_not_exist.py"])

    def fail_run(*args, **kwargs):
        raise AssertionError("no tool may run for unknown input paths")

    monkeypatch.setattr(module.subprocess, "run", fail_run)

    assert module.main() == 2

    captured = capsys.readouterr()
    assert "core/does_not_exist.py" in captured.out


def test_main_rejects_direct_file_without_registered_capability(monkeypatch, capsys):
    module = _load_quality_module()

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "webui/package.json"])

    def fail_run(*args, **kwargs):
        raise AssertionError("no tool may receive a file without a registered capability")

    monkeypatch.setattr(module.subprocess, "run", fail_run)

    assert module.main() == 2

    captured = capsys.readouterr()
    assert "webui/package.json" in captured.out


def _fake_which(available: set[str]):
    return lambda name: name if name in available else None


def test_main_routes_lifecycle_scripts_to_native_syntax_checks(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        module.sys, "argv", ["quality.py", "scripts/uninstall.ps1", "scripts/uninstall.sh"]
    )
    monkeypatch.setattr(module.shutil, "which", _fake_which({"bash", "pwsh"}))

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        stdout = "66 passed in 0.01s\n" if cmd[2:3] == ["pytest"] else ""
        return module.subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    output = capsys.readouterr().out
    # An empty Ruff target list would lint the whole repository instead.
    assert not any(cmd[2:3] in (["ruff"], ["mypy"]) for cmd in commands)
    for label in ("ruff format", "ruff fix", "ruff check", "mypy"):
        line = next(line for line in output.splitlines() if line.startswith(label))
        assert "NO FILES" in line
    assert ["bash", "-n", "scripts/uninstall.sh"] in commands
    powershell = next(cmd for cmd in commands if cmd[0] == "pwsh")
    assert "'scripts/uninstall.ps1'" in powershell[-1]
    assert "scripts/uninstall.sh" not in powershell[-1]
    pytest_command = next(cmd for cmd in commands if cmd[2:3] == ["pytest"])
    assert pytest_command[-1] == "tests/scripts/test_install_scripts.py"
    assert "sh syntax     .... PASS" in output
    assert "ps1 syntax    .... PASS" in output


def test_main_keeps_scripts_out_of_python_tools_in_mixed_scope(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        module.sys, "argv", ["quality.py", "--check", "scripts/quality.py", "scripts/setup.sh"]
    )
    monkeypatch.setattr(module.shutil, "which", _fake_which({"bash"}))

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        stdout = "2 passed in 0.01s\n" if cmd[2:3] == ["pytest"] else ""
        return module.subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    capsys.readouterr()
    assert next(cmd for cmd in commands if cmd[2:4] == ["ruff", "format"])[5:] == [
        "scripts/quality.py"
    ]
    assert next(cmd for cmd in commands if cmd[2:3] == ["mypy"])[4:] == ["scripts/quality.py"]
    assert ["bash", "-n", "scripts/setup.sh"] in commands
    assert next(cmd for cmd in commands if cmd[2:3] == ["pytest"])[-2:] == [
        "tests/scripts/test_quality.py",
        "tests/scripts/test_install_scripts.py",
    ]


def test_main_reports_skipped_syntax_check_without_interpreter(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(module.sys, "argv", ["quality.py", "scripts/install.sh"])
    monkeypatch.setattr(module.shutil, "which", _fake_which(set()))

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        return module.subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    output = capsys.readouterr().out
    syntax_line = next(line for line in output.splitlines() if line.startswith("sh syntax"))
    assert "SKIPPED" in syntax_line
    assert "PASS" not in syntax_line
    assert "note: scripts/install.sh" in output
    assert [cmd[0] for cmd in commands] == [module.sys.executable]


def test_main_fails_on_script_syntax_error(monkeypatch, capsys):
    module = _load_quality_module()
    monkeypatch.setattr(module.sys, "argv", ["quality.py", "--check", "scripts/setup.ps1"])
    monkeypatch.setattr(module.shutil, "which", _fake_which({"powershell"}))

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[0] == "powershell":
            return module.subprocess.CompletedProcess(
                cmd, 1, stdout="scripts/setup.ps1:3:1: Missing closing '}'\n", stderr=""
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    output = capsys.readouterr().out
    assert "ps1 syntax    .... FAIL" in output
    assert "--- ps1 syntax ---" in output
    assert "scripts/setup.ps1:3:1: Missing closing '}'" in output


def test_main_checks_every_shell_script_after_a_failure(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(
        module.sys, "argv", ["quality.py", "scripts/install.sh", "scripts/setup.sh"]
    )
    monkeypatch.setattr(module.shutil, "which", _fake_which({"bash"}))

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        if cmd == ["bash", "-n", "scripts/install.sh"]:
            return module.subprocess.CompletedProcess(
                cmd, 2, stdout="", stderr="scripts/install.sh: syntax error\n"
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    output = capsys.readouterr().out
    assert [cmd for cmd in commands if cmd[0] == "bash"] == [
        ["bash", "-n", "scripts/install.sh"],
        ["bash", "-n", "scripts/setup.sh"],
    ]
    assert "sh syntax     .... FAIL" in output
    assert "sh syntax     .... PASS" in output
    assert "note: scripts/install.sh" in output
    assert "note: scripts/setup.sh" in output
    assert "scripts/install.sh: syntax error" in output


def test_bash_syntax_checks_every_script(tmp_path):
    module = _load_quality_module()
    bash = module.shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    (tmp_path / "good.sh").write_text("touch executed.txt\n", encoding="utf-8")
    (tmp_path / "bad.sh").write_text("if then fi (\n", encoding="utf-8")
    (tmp_path / "also good.sh").write_text("echo ok\n", encoding="utf-8")

    # `bash -n good.sh bad.sh` only parses good.sh. Every file needs a process.
    steps = module._native_script_steps(["good.sh", "bad.sh", "also good.sh"])
    results = [
        module.subprocess.run(
            step.command, cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        for step in steps
    ]

    assert len(results) == 3
    assert results[0].returncode == 0
    assert results[1].returncode != 0
    assert "bad.sh" in results[1].stderr
    assert results[2].returncode == 0
    assert not (tmp_path / "executed.txt").exists()


def test_powershell_parse_command_reports_errors_per_script(tmp_path):
    module = _load_quality_module()
    powershell = module.shutil.which("pwsh") or module.shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    (tmp_path / "good.ps1").write_text("Write-Output 'ok'\n", encoding="utf-8")
    (tmp_path / "bad.ps1").write_text("function Broken {\n  if ($true) {\n", encoding="utf-8")

    passing = module.subprocess.run(
        module._powershell_parse_command(powershell, ["good.ps1"]),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    failing = module.subprocess.run(
        module._powershell_parse_command(powershell, ["good.ps1", "bad.ps1"]),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert passing.returncode == 0, passing.stdout + passing.stderr
    assert failing.returncode == 1
    assert failing.stdout.startswith("bad.ps1:")
    assert "good.ps1" not in failing.stdout


def test_translate_maps_lifecycle_scripts_to_install_script_tests():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(
        ["scripts/install.ps1", "scripts/setup.sh", "scripts/windows/smoke_installer.ps1"]
    )

    assert test_paths == ["tests/scripts/test_install_scripts.py"]
    assert len(notes) == 1
    assert "scripts/windows/smoke_installer.ps1" in notes[0]


def test_main_routes_python_config_to_full_pipeline(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "pyproject.toml"])
    monkeypatch.setattr(module, "snapshot_target_files", lambda *args, **kwargs: {})

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    capsys.readouterr()
    assert next(cmd for cmd in commands if cmd[2:4] == ["ruff", "format"])[4:] == ["."]
    assert next(cmd for cmd in commands if cmd[2] == "mypy")[4:] == module.FULL_MYPY_PATHS
    assert next(cmd for cmd in commands if cmd[2] == "pytest")[-1] == "tests/"


def test_main_reports_no_tests_when_pytest_collects_none(monkeypatch, capsys):
    module = _load_quality_module()
    # A mirrored test file exists, but pytest collects nothing (exit code 5).
    monkeypatch.setattr(module.sys, "argv", ["quality.py", "core/prompts/prompts.py"])

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[2] == "pytest":
            return module.subprocess.CompletedProcess(
                cmd, 5, stdout="no tests ran in 0.01s\n", stderr=""
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    captured = capsys.readouterr()
    pytest_line = next(line for line in captured.out.splitlines() if line.startswith("pytest"))
    assert "NO TESTS" in pytest_line
    assert "PASS" not in pytest_line


def test_main_fails_when_fix_step_crashes(monkeypatch, capsys):
    module = _load_quality_module()

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "core/prompts/prompts.py"])

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[2:4] == ["ruff", "format"]:
            return module.subprocess.CompletedProcess(
                cmd, 2, stdout="", stderr="error: ruff crashed"
            )
        if cmd[2] == "pytest":
            return module.subprocess.CompletedProcess(
                cmd, 0, stdout="1 passed in 0.01s\n", stderr=""
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    captured = capsys.readouterr()
    assert "error: ruff crashed" in captured.out
    assert "All gates passed" not in captured.out
