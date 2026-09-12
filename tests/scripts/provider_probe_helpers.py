"""Provider diagnostic probe support coverage."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import scripts.provider_probe.common as probe_common

PROJECT_ROOT = Path(__file__).resolve().parents[2]


MODULE_PATH = PROJECT_ROOT / "scripts" / "probe_provider_tool_call.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provider_tool_call_probe", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_module()


def expected_arguments(scenario: probe_common.ProbeScenario) -> dict[str, Any]:
    assert scenario.expected_arguments is not None
    return scenario.expected_arguments
