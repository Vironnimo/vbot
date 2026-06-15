"""Tests for the standalone ``scripts/validate_model_db.py`` thin wrapper.

The detection logic itself lives in ``core/models/validation.py`` and is unit-
tested in ``tests/core/models/test_validation.py``. Here we only confirm the
script wires findings to the right exit code.
"""

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "validate_model_db.py"
VALIDATOR_FIXTURES = PROJECT_ROOT / "tests" / "core" / "models" / "fixtures" / "validator"


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_model_db", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_returns_one_when_findings_exist():
    module = _load_module()
    assert module.main(["--resources", str(VALIDATOR_FIXTURES)]) == 1


def test_main_returns_zero_on_clean_db(tmp_path: Path):
    module = _load_module()
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "models.json").write_text('{"models": {}}', encoding="utf-8")

    assert module.main(["--resources", str(tmp_path)]) == 0


def test_main_returns_two_on_missing_models_dir(tmp_path: Path):
    module = _load_module()
    assert module.main(["--resources", str(tmp_path)]) == 2
