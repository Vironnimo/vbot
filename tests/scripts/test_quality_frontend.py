import importlib.util
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "quality-frontend.py"


def _load_quality_frontend_module():
    spec = importlib.util.spec_from_file_location("quality_frontend", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_vitest_counts_strips_ansi_color_codes():
    module = _load_quality_frontend_module()
    output = (
        "\x1b[1m\x1b[30m\x1b[46m RUN \x1b[49m\x1b[39m\x1b[22m\n"
        "\n"
        " \x1b[32m✓\x1b[39m src/components/__tests__/AgentsView.test.js\n"
        "\n"
        " Test Files  \x1b[32m1 passed\x1b[39m (1)\n"
        "      Tests  \x1b[32m16 passed\x1b[39m (16)\n"
        "   Start at  22:02:18\n"
    )

    assert module.parse_vitest_counts(output) == (16, 16)


def test_parse_vitest_counts_returns_zero_without_summary_line():
    module = _load_quality_frontend_module()

    assert module.parse_vitest_counts("\x1b[32m✓\x1b[39m test output without summary") == (0, 0)


def test_translate_to_vitest_targets_keeps_explicit_test_file():
    module = _load_quality_frontend_module()

    assert module.translate_to_vitest_targets(
        ["src/components/__tests__/SettingsView.test.js"]
    ) == ["src/components/__tests__/SettingsView.test.js"]


def test_translate_to_vitest_targets_uses_parent_dir_for_non_test_file():
    module = _load_quality_frontend_module()

    assert module.translate_to_vitest_targets(["src/components/SettingsView.svelte"]) == [
        "src/components"
    ]


def test_filter_vitest_failure_output_removes_pass_noise():
    module = _load_quality_frontend_module()
    output = (
        "\x1b[1m\x1b[46m RUN \x1b[49m\x1b[22m v4.1.5 C:/Development/projects/vBot/webui\n"
        "\n"
        " \x1b[32m✓\x1b[39m src/components/__tests__/SettingsView.test.js (2 tests) 30ms\n"
        "   \x1b[32m✓\x1b[39m SettingsView (2)\n"
        "     \x1b[32m✓\x1b[39m saves automatically\n"
        "     \x1b[31m×\x1b[39m keeps dirty state on error 15ms\n"
        "\n"
        "\x1b[31mFAIL\x1b[39m src/components/__tests__/SettingsView.test.js > "
        "SettingsView > keeps dirty state on error\n"
        "AssertionError: expected false to be true\n"
        "\n"
        " Test Files  1 failed | 1 passed (2)\n"
        "      Tests  1 failed | 3 passed (4)\n"
        "   Duration  1.20s\n"
    )

    filtered = module.filter_vitest_failure_output(output)

    assert "RUN  v4.1.5" not in filtered
    assert "saves automatically" not in filtered
    assert (
        "FAIL src/components/__tests__/SettingsView.test.js > "
        "SettingsView > keeps dirty state on error" in filtered
    )
    assert "AssertionError: expected false to be true" in filtered
    assert "Tests  1 failed | 3 passed (4)" in filtered


def test_main_runs_vitest_with_verbose_reporter(monkeypatch, capsys):
    module = _load_quality_frontend_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["quality-frontend.py", "webui/src/components/__tests__/AgentsView.test.js"],
    )

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        commands.append(cmd)
        if cmd[1] == "vitest":
            return module.subprocess.CompletedProcess(
                cmd, 0, stdout="Tests  1 passed (1)\n", stderr=""
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 0

    capsys.readouterr()
    vitest_command = next(cmd for cmd in commands if cmd[1] == "vitest")
    assert vitest_command[:5] == [
        "npx",
        "vitest",
        "run",
        "--reporter=verbose",
        "--passWithNoTests",
    ]
    assert vitest_command[-1] == "src/components/__tests__/AgentsView.test.js"


def test_main_filters_vitest_failure_output(monkeypatch, capsys):
    module = _load_quality_frontend_module()
    vitest_output = (
        "\x1b[1m\x1b[46m RUN \x1b[49m\x1b[22m v4.1.5 C:/Development/projects/vBot/webui\n"
        " \x1b[32m✓\x1b[39m src/components/__tests__/SettingsView.test.js (2 tests) 30ms\n"
        "   \x1b[32m✓\x1b[39m SettingsView (2)\n"
        "     \x1b[32m✓\x1b[39m saves automatically\n"
        "     \x1b[31m×\x1b[39m keeps dirty state on error 15ms\n"
        "\n"
        "\x1b[31mFAIL\x1b[39m src/components/__tests__/SettingsView.test.js > "
        "SettingsView > keeps dirty state on error\n"
        "AssertionError: expected false to be true\n"
        "\n"
        " Test Files  1 failed | 1 passed (2)\n"
        "      Tests  1 failed | 3 passed (4)\n"
    )

    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        module.sys, "argv", ["quality-frontend.py", "webui/src/components/AgentsView.svelte"]
    )

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[1] == "vitest":
            return module.subprocess.CompletedProcess(cmd, 1, stdout=vitest_output, stderr="")
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    captured = capsys.readouterr()
    assert "--- vitest ---" in captured.out
    assert "saves automatically" not in captured.out
    assert (
        "FAIL src/components/__tests__/SettingsView.test.js > "
        "SettingsView > keeps dirty state on error" in captured.out
    )


def test_parse_vitest_counts_handles_skipped_segment():
    module = _load_quality_frontend_module()
    output = " Test Files  2 passed (2)\n      Tests  1 skipped | 4 passed (5)\n"

    assert module.parse_vitest_counts(output) == (4, 5)


def test_main_rejects_unknown_input_path(monkeypatch, capsys):
    module = _load_quality_frontend_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        module.sys, "argv", ["quality-frontend.py", "webui/src/components/Missing.svelte"]
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("no tool may run for unknown input paths")

    monkeypatch.setattr(module.subprocess, "run", fail_run)

    assert module.main() == 2

    captured = capsys.readouterr()
    assert "ERROR: path not found under webui/: src/components/Missing.svelte" in captured.out


def _full_scan_run_factory(build_returncode, build_stdout, build_stderr):
    """Return a fake ``subprocess.run`` for a full-scan gate run.

    Every step passes except the build, which is given the supplied result so a
    test can exercise a clean build, a warning-emitting build, or a failing one.
    """

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[:3] == ["npm", "run", "build"]:
            return subprocess.CompletedProcess(
                cmd, build_returncode, stdout=build_stdout, stderr=build_stderr
            )
        if cmd[1] == "vitest":
            return subprocess.CompletedProcess(cmd, 0, stdout="Tests  1 passed (1)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_main_surfaces_build_warnings_on_success(monkeypatch, capsys):
    module = _load_quality_frontend_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(module.sys, "argv", ["quality-frontend.py"])

    # vite routes the asset list to stdout and warnings to stderr; the build
    # still exits 0. The warning carries ANSI color codes, like the real tool.
    build_stdout = "dist/assets/index.js  701.14 kB\n✓ built in 2.0s\n"
    build_stderr = "\x1b[33m\n(!) Some chunks are larger than 800 kB after minification.\x1b[39m"

    monkeypatch.setattr(
        module.subprocess,
        "run",
        _full_scan_run_factory(0, build_stdout, build_stderr),
    )

    assert module.main() == 0

    captured = capsys.readouterr()
    assert "build" in captured.out
    assert "PASS" in captured.out and "warnings" in captured.out
    assert "--- build (warnings) ---" in captured.out
    assert "(!) Some chunks are larger than 800 kB after minification." in captured.out
    # ANSI codes are stripped from the surfaced warning.
    assert "\x1b[33m" not in captured.out
    # Warnings do not fail the gate, and the asset-list noise stays hidden.
    assert "All gates passed" in captured.out
    assert "701.14 kB" not in captured.out


def test_main_hides_build_output_on_clean_success(monkeypatch, capsys):
    module = _load_quality_frontend_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(module.sys, "argv", ["quality-frontend.py"])

    build_stdout = "dist/assets/index.js  701.14 kB\n✓ built in 2.0s\n"

    monkeypatch.setattr(
        module.subprocess,
        "run",
        _full_scan_run_factory(0, build_stdout, ""),
    )

    assert module.main() == 0

    captured = capsys.readouterr()
    assert "All gates passed" in captured.out
    assert "warnings" not in captured.out
    assert "--- build (warnings) ---" not in captured.out
    # A clean build prints only its PASS line, no asset-list noise.
    assert "701.14 kB" not in captured.out


def test_main_fails_when_fix_step_crashes(monkeypatch, capsys):
    module = _load_quality_frontend_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        module.sys, "argv", ["quality-frontend.py", "webui/src/components/AgentsView.svelte"]
    )

    def fake_run(cmd, capture_output, text, cwd, encoding, errors):
        if cmd[1] == "prettier":
            return module.subprocess.CompletedProcess(
                cmd, 2, stdout="", stderr="prettier internal error"
            )
        if cmd[1] == "vitest":
            return module.subprocess.CompletedProcess(
                cmd, 0, stdout="Tests  1 passed (1)\n", stderr=""
            )
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.main() == 1

    captured = capsys.readouterr()
    assert "prettier internal error" in captured.out
    assert "All gates passed" not in captured.out
