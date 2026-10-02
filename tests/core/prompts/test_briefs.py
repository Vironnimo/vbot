"""Learning-Run briefs: assembly from shared prompt fragments."""

from __future__ import annotations

from pathlib import Path

from core.prompts.briefs import (
    BRIEF_FRAGMENT_NAMES,
    REVIEW_TOOL_CALL_LIMIT,
    learn_brief,
    reflection_brief,
)
from core.storage import StorageManager


def test_briefs_compose_per_fragment_overrides_and_share_each_fragment(tmp_path: Path) -> None:
    storage = StorageManager(data_dir=tmp_path / "data")
    storage.ensure_directories()
    # Each fragment resolves on its own, so a data-dir copy of one shared
    # fragment reaches every brief that uses it.
    for name in BRIEF_FRAGMENT_NAMES:
        (storage.prompts_dir / name).write_text(f"[{name}]\n", encoding="utf-8")
    (storage.prompts_dir / "review-intro-memory.md").write_text(
        "[review-intro-memory.md {tool_call_limit}]\n", encoding="utf-8"
    )

    # Groups are paragraphs; list fragments continue their lead line by line.
    signals_end = "[review-signals-end.md]"
    skill_part = (
        "[skill-shape.md]\n\n[skill-ladder.md]\n\n[skill-read-current.md] [review-skill-limits.md]"
    )
    assert reflection_brief(storage, "memory") == (
        f"[review-intro-memory.md {REVIEW_TOOL_CALL_LIMIT}]"
        "\n\n[review-signals-lead.md]\n[review-signals-memory.md]"
        f"\n\n{signals_end}"
        "\n\n[learning-routing.md] [review-routing-memory-only.md]"
        "\n\n[review-skip.md]\n\n[review-memory.md]\n\n[review-closing.md]"
    )
    assert reflection_brief(storage, "skill") == (
        "[review-intro-skill.md]"
        "\n\n[review-signals-lead.md]\n[review-signals-skill.md]"
        f"\n\n{signals_end}"
        "\n\n[learning-routing.md] [review-routing-skill-only.md]"
        f"\n\n[review-skip.md]\n\n{skill_part}\n\n[review-closing.md]"
    )
    combined = (
        "[review-intro-combined.md]"
        "\n\n[review-signals-lead.md]\n[review-signals-skill.md]\n[review-signals-memory.md]"
        f"\n\n{signals_end}"
        "\n\n[learning-routing.md]"
        f"\n\n[review-skip.md]\n\n[review-memory.md]\n\n{skill_part}\n\n[review-closing.md]"
    )
    assert reflection_brief(storage, "combined") == combined
    assert reflection_brief(storage, "combined", focus="  the deploy Skill \n") == (
        f"{combined}\n\nThe user asked you to focus this reflection on:\nthe deploy Skill"
    )
    learn = (
        "[learn-intro.md]\n\n[skill-shape.md]\n\n[skill-ladder.md]\n\n[skill-read-current.md]"
        "\n\n[learn-method.md]"
    )
    assert learn_brief(storage, " deploy steps ") == (
        f"{learn}\n\nThe request to learn from:\ndeploy steps"
    )
    assert learn_brief(storage, None) == (
        f"{learn}\n\nNo request was given. If the recent conversation clearly establishes "
        "reusable learning, apply the instructions above to it. Otherwise, ask the user what "
        "they want captured."
    )
