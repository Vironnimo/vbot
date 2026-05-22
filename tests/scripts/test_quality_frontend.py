import importlib.util
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
        ["quality-frontend.py", "webui/src/components/__tests__/Foo.test.js"],
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
    assert vitest_command[:4] == ["npx", "vitest", "run", "--reporter=verbose"]
    assert vitest_command[-1] == "src/components/__tests__/Foo.test.js"


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
        module.sys, "argv", ["quality-frontend.py", "webui/src/components/Foo.svelte"]
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
