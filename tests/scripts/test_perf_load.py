"""Command line of scripts/perf_load.py (no server is started here)."""

import json

import pytest

import scripts.perf_load as perf_load
from scripts.perf_load_suite.report import RESULT_KIND
from scripts.perf_load_suite.runner import DEFAULT_LEVELS, DEFAULT_OUTPUT_ROOT, QUICK_LEVELS


def _config(*argv):
    return perf_load.config_from_args(perf_load.build_run_parser().parse_args(list(argv)))


def test_defaults_describe_the_full_load_scenario():
    config = _config()

    assert config.levels == DEFAULT_LEVELS
    assert (config.turns, config.steps, config.tokens, config.rate, config.think_ms) == (
        3,
        4,
        400,
        80.0,
        600,
    )
    assert config.tools == ("read", "search_files", "bash")
    assert config.output_root == DEFAULT_OUTPUT_ROOT
    assert not (config.profile or config.ui or config.keep)


def test_quick_overrides_levels_and_turns():
    config = _config("--quick", "--agents", "2,4", "--turns", "5")

    assert (config.levels, config.turns) == (QUICK_LEVELS, 1)


def test_gil_profile_implies_profiling():
    config = _config("--profile-gil", "--agents", "1,20")

    assert (config.profile, config.profile_gil, config.profiled_level) == (True, True, 20)


@pytest.mark.parametrize(
    "argv", [["--agents", "1,x"], ["--agents", "0"], ["--agents", "3,3"], ["--tools", ","]]
)
def test_malformed_lists_are_usage_errors(argv):
    with pytest.raises(SystemExit) as caught:
        perf_load.build_run_parser().parse_args(argv)

    assert caught.value.code == 2


def test_invalid_directive_values_are_usage_errors():
    with pytest.raises(SystemExit) as caught:
        perf_load.main(["--steps", "0"])

    assert caught.value.code == 2


def _write_result(path, ttft_p50):
    path.write_text(
        json.dumps(
            {
                "kind": RESULT_KIND,
                "started_at": path.stem,
                "git": {"commit": "c"},
                "levels": [{"agents": 1, "client": {"ttft_overhead_ms": {"p50": ttft_p50}}}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_compare_subcommand_prints_the_difference(tmp_path, capsys):
    old = _write_result(tmp_path / "old.json", 100.0)
    new = _write_result(tmp_path / "new.json", 150.0)

    assert perf_load.main(["compare", str(old), str(new)]) == 0

    output = capsys.readouterr().out
    assert "client.ttft_overhead_ms.p50" in output
    assert "+50.0%" in output


def test_compare_subcommand_rejects_unreadable_input(tmp_path, capsys):
    new = _write_result(tmp_path / "new.json", 150.0)

    assert perf_load.main(["compare", str(tmp_path / "missing.json"), str(new)]) == 2
    assert "error:" in capsys.readouterr().err


def test_compare_option_rejects_a_bad_baseline_before_running(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        perf_load.main(["--compare", str(bad)])

    assert caught.value.code == 2


@pytest.mark.parametrize("status, expected_exit", [("ok", 0), ("failed", 1)])
def test_run_exit_code_reflects_level_outcome(monkeypatch, tmp_path, status, expected_exit):
    result = {"levels": [{"agents": 1, "status": status}]}
    monkeypatch.setattr(perf_load, "activate_process_containment", lambda: None)
    monkeypatch.setattr(perf_load, "run_load", lambda config: (tmp_path, result))

    assert perf_load.main(["--agents", "1"]) == expected_exit
