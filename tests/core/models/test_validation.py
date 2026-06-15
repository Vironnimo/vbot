"""Tests for the standalone Model DB validator detection logic.

The validator is offline (not on the runtime read path). It flags dead canonical
pointers and redundant manual joins. The fixtures under ``fixtures/validator/``
carry exactly one deliberately dead pointer and one deliberately redundant manual
join, plus a clean valid manual pointer that must NOT be flagged.
"""

from pathlib import Path

from core.models.validation import (
    DEAD_POINTER,
    REDUNDANT_MANUAL_JOIN,
    validate_model_db,
)

VALIDATOR_FIXTURES = Path(__file__).parent / "fixtures" / "validator"


class TestValidateModelDb:
    def test_flags_the_deliberately_dead_pointer(self):
        findings = validate_model_db(VALIDATOR_FIXTURES)

        dead = [f for f in findings if f.kind == DEAD_POINTER]
        assert len(dead) == 1
        assert dead[0].provider_id == "opencode-go"
        assert dead[0].wire_id == "deepseek-v4-pro"
        assert dead[0].pointer == "deepseek/deepseek-v4-renamed"

    def test_flags_the_deliberately_redundant_manual_join(self):
        findings = validate_model_db(VALIDATOR_FIXTURES)

        redundant = [f for f in findings if f.kind == REDUNDANT_MANUAL_JOIN]
        assert len(redundant) == 1
        assert redundant[0].provider_id == "openrouter"
        assert redundant[0].wire_id == "deepseek/deepseek-v4-pro"
        assert redundant[0].pointer == "deepseek/deepseek-v4-pro"

    def test_does_not_flag_a_valid_non_redundant_manual_pointer(self):
        """``openrouter/vendor-x/clean-mapped`` carries a valid manual pointer
        whose target exists and does not equal the wire-id — no finding."""

        findings = validate_model_db(VALIDATOR_FIXTURES)

        offenders = {(f.provider_id, f.wire_id) for f in findings}
        assert ("openrouter", "vendor-x/clean-mapped") not in offenders

    def test_total_finding_count_is_exactly_two(self):
        findings = validate_model_db(VALIDATOR_FIXTURES)
        assert len(findings) == 2

    def test_clean_db_has_no_findings(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "models.json").write_text(
            '{"models": {"lab/x": {"name": "X"}}}', encoding="utf-8"
        )
        (models_dir / "p.json").write_text(
            """
            {
              "provider_id": "p",
              "models": {
                "wire-1": {"name": "W1", "canonical": "lab/x"}
              }
            }
            """,
            encoding="utf-8",
        )

        assert validate_model_db(tmp_path) == []

    def test_auto_pointer_equal_to_wire_id_is_not_redundant(self, tmp_path: Path):
        """The redundancy check is for MANUAL (override) pointers only. An auto
        pointer in ``<provider>.json`` that equals the wire-id is not flagged —
        only a hand-written redundant join is."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "models.json").write_text(
            '{"models": {"lab/model": {"name": "X"}}}', encoding="utf-8"
        )
        (models_dir / "openrouter.json").write_text(
            """
            {
              "provider_id": "openrouter",
              "models": {
                "lab/model": {"name": "M", "canonical": "lab/model"}
              }
            }
            """,
            encoding="utf-8",
        )

        assert validate_model_db(tmp_path) == []
