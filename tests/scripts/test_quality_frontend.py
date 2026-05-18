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
