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

    def fake_run(cmd, capture_output, text):
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

    monkeypatch.setattr(module.sys, "argv", ["quality.py", "tests/example/test_demo.py"])

    def fake_run(cmd, capture_output, text):
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
