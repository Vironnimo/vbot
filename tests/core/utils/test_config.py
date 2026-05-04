"""Tests for configuration and dotenv helpers."""

from core.utils.config import parse_env_lines


def test_parse_env_lines_keeps_values_conservative() -> None:
    """Dotenv parsing keeps only simple key-value behavior."""
    lines = [
        "# comment",
        "",
        "IGNORED",
        "OPENROUTER_API_KEY=sk-or-test=value",
        "QUOTED='quoted value'",
    ]

    values = parse_env_lines(lines)

    assert values == {
        "OPENROUTER_API_KEY": "sk-or-test=value",
        "QUOTED": "quoted value",
    }
