"""Models: the offline Model DB validator (dead pointers, redundant manual joins).

The fixtures under ``fixtures/validator/`` carry one dead pointer, one redundant
manual join, and one valid manual pointer that must not be flagged.
"""

from pathlib import Path

import pytest

from core.models.validation import (
    DEAD_POINTER,
    REDUNDANT_MANUAL_JOIN,
    validate_model_db,
)

VALIDATOR_FIXTURES = Path(__file__).parent / "fixtures" / "validator"


def test_fixture_db_reports_exactly_the_planted_findings() -> None:
    findings = validate_model_db(VALIDATOR_FIXTURES)

    assert [(f.kind, f.provider_id, f.wire_id, f.pointer) for f in findings] == [
        (DEAD_POINTER, "opencode-go", "deepseek-v4-pro", "deepseek/deepseek-v4-renamed"),
        (
            REDUNDANT_MANUAL_JOIN,
            "openrouter",
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-pro",
        ),
    ]


@pytest.mark.parametrize(
    ("canonical_id", "wire_id"),
    [
        ("lab/x", "wire-1"),
        # Redundancy applies to manual (override) pointers only, not auto pointers.
        ("lab/model", "lab/model"),
    ],
    ids=["valid-auto-pointer", "auto-pointer-equal-to-wire-id"],
)
def test_clean_db_has_no_findings(tmp_path: Path, canonical_id: str, wire_id: str) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "models.json").write_text(
        f'{{"models": {{"{canonical_id}": {{"name": "X"}}}}}}', encoding="utf-8"
    )
    (models_dir / "p.json").write_text(
        f'{{"provider_id": "p", "models": {{"{wire_id}": '
        f'{{"name": "W", "canonical": "{canonical_id}"}}}}}}',
        encoding="utf-8",
    )

    assert validate_model_db(tmp_path) == []
