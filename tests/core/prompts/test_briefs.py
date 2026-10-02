"""Learning-Run briefs: assembly from shared prompt fragments."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.prompts.briefs import BRIEF_FRAGMENT_NAMES, learn_brief, reflection_brief
from core.storage import StorageManager

# SHA-256 of the whole-brief resources the assembled briefs replaced, stripped
# like their readers stripped them (``git show 55ce9217f:resources/prompts/<file>``).
# Transitional: delete this table and its test when the brief texts are rewritten.
_REPLACED_BRIEF_SHA256 = {
    "memory": "4d80a207ed4aa9b03ea8c3fd8d57383031cdccaa055b4376e73330c4e9255e05",
    "skill": "5097b88e55eb6b419268b42307697d6a7859f88fba4bea54fe70fd1704b7c5c1",
    "combined": "096b4ecc03733d1da68edcc5da2e9bfbff78a5600f5d60636410d9cc88098154",
    "learn": "b61c56c4c91948e510a18664ddb0132b598d7e1d2681717cef25d496c852a491",
}


@pytest.mark.parametrize("brief", ["memory", "skill", "combined", "learn"])
def test_bundled_briefs_reproduce_the_whole_brief_texts_they_replaced(
    tmp_path: Path, brief: str
) -> None:
    bundled = StorageManager(data_dir=tmp_path / "data")

    text = (
        learn_brief(bundled, "req").removesuffix("\n\nThe request to learn from:\nreq")
        if brief == "learn"
        else reflection_brief(bundled, brief)  # type: ignore[arg-type]
    )

    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _REPLACED_BRIEF_SHA256[brief]


def test_briefs_compose_per_fragment_overrides_and_share_each_fragment(tmp_path: Path) -> None:
    storage = StorageManager(data_dir=tmp_path / "data")
    storage.ensure_directories()
    # Each fragment resolves on its own, so a data-dir copy of one shared
    # fragment reaches every brief that uses it.
    for name in BRIEF_FRAGMENT_NAMES:
        (storage.prompts_dir / name).write_text(f"[{name}]\n", encoding="utf-8")

    assert reflection_brief(storage, "memory") == (
        "[reflect-memory-intro.md] [reflect-memory-method.md]\n\n[reflect-memory-closing.md]"
    )
    assert reflection_brief(storage, "skill") == (
        "[reflect-skill-intro.md] [skill-ownership-check.md] [reflect-skill-method.md]"
        "\n\n[reflect-skill-closing.md]"
    )
    combined = (
        "[reflect-combined-intro.md] [reflect-memory-method.md]"
        "\n\n[reflect-combined-skill-lead.md] [skill-ownership-check.md] [reflect-skill-method.md]"
        "\n\n[reflect-combined-closing.md]"
    )
    assert reflection_brief(storage, "combined") == combined
    assert reflection_brief(storage, "combined", focus="  the deploy Skill \n") == (
        f"{combined}\n\nThe user asked you to focus this reflection on:\nthe deploy Skill"
    )
    learn = "[learn-intro.md] [skill-ownership-check.md] [learn-method.md]"
    assert learn_brief(storage, " deploy steps ") == (
        f"{learn}\n\nThe request to learn from:\ndeploy steps"
    )
    assert learn_brief(storage, None) == (
        f"{learn}\n\nNo request was given. If the recent conversation clearly establishes "
        "reusable learning, apply the instructions above to it. Otherwise, ask the user what "
        "they want captured."
    )
