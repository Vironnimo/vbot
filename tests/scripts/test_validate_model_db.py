"""Smoke test of the ``scripts/validate_model_db.py`` wrapper.

The detection logic lives in ``core/models/validation.py`` and is tested with its owner.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

VALIDATOR_FIXTURES = (
    Path(__file__).resolve().parents[2] / "tests" / "core" / "models" / "fixtures" / "validator"
)


def test_validator_exits_nonzero_when_the_model_db_has_findings() -> None:
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "validate_model_db.py"
    spec = importlib.util.spec_from_file_location("validate_model_db", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--resources", str(VALIDATOR_FIXTURES)]) == 1
