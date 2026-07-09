import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "_quality_common.py"


def _load_common_module():
    spec = importlib.util.spec_from_file_location("quality_common", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_describe_fix_result_reports_fixed_file_count():
    module = _load_common_module()

    assert module.describe_fix_result(0, 1.2, ["a.py", "b.py"]) == "FIXED (1.2s, 2 files)"
    assert module.describe_fix_result(0, 0.5, ["a.py"]) == "FIXED (0.5s, 1 file)"


def test_describe_fix_result_says_no_changes_instead_of_pass():
    module = _load_common_module()

    # A fix step that changed nothing did nothing — it must not read as "PASS".
    status = module.describe_fix_result(0, 0.7, [])

    assert status == "NO CHANGES (0.7s)"
    assert "PASS" not in status


def test_describe_fix_result_reports_unfixable_remainder():
    module = _load_common_module()

    # Exit code 1 = unfixable issues remain for the follow-up gate step.
    status = module.describe_fix_result(1, 0.3, [])

    assert status == "UNCHANGED (0.3s, no automatic fixes applied)"
