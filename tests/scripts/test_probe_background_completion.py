"""Smoke test of the live background-completion Provider probe CLI."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "probe_background_completion.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("background_completion_probe", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_module()


def test_probe_reports_targets_without_usable_credentials_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Runtime:
        def __init__(self, _config: object) -> None:
            self.provider_credentials = SimpleNamespace(is_usable=lambda *_route: False)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(PROBE, "Runtime", Runtime)
    monkeypatch.setattr(PROBE, "Config", lambda **_: None)
    monkeypatch.setattr(PROBE, "_start_probe_runtime", lambda _runtime: None)

    code = asyncio.run(PROBE._run(PROBE._parser().parse_args([])))

    report = json.loads(capsys.readouterr().out)
    assert (code, report["passed"]) == (1, False)
    assert report["cases"] == len(PROBE.TARGETS)
    assert {result["status"] for result in report["results"]} == {"unavailable"}
