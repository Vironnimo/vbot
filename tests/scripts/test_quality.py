import importlib.util
from pathlib import Path

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

    assert test_paths == ["tests/core/prompts/test_prompts.py"]
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

    # The oauth split sibling has no own source file, so it belongs to the
    # adapter and must run alongside the exact mirror.
    assert "tests/core/providers/test_openai_compatible.py" in test_paths
    assert "tests/core/providers/test_openai_compatible_oauth.py" in test_paths
    assert notes == []


def test_translate_does_not_claim_more_specific_siblings():
    module = _load_quality_module()

    test_paths, notes = module.translate_to_test_paths(["core/providers/openai.py"])

    # openai.py must not pull in openai_compatible's mirrors: the longer source
    # stem owns them.
    assert test_paths == ["tests/core/providers/test_openai.py"]
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
    assert "ERROR: path not found: core/does_not_exist.py" in captured.out


def test_main_skips_pytest_without_mirrored_tests(monkeypatch, capsys):
    module = _load_quality_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "webui/package.json"])

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    captured = capsys.readouterr()
    assert "SKIP (no mirrored tests)" in captured.out
    assert "note: webui/package.json" in captured.out
    assert not any(cmd[2] == "pytest" for cmd in commands)


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
