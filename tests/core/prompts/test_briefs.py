"""Learning-Run briefs: assembly from shared prompt fragments."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.prompts.briefs import (
    BRIEF_FRAGMENT_NAMES,
    REVIEW_TOOL_CALL_LIMIT,
    LibrarianCandidate,
    learn_brief,
    librarian_brief,
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
    # A Librarian copy without the candidates marker still lists the candidates.
    assert librarian_brief(storage, [_CANDIDATE], limit=60) == (
        f"[librarian.md]\n\n{_CANDIDATE_TEXT}"
    )


_CANDIDATE = LibrarianCandidate(
    name="deploy-vercel",
    description="Deploy the web app\n  to Vercel.",
    origin="reflection",
    created="2026-05-01",
    changed="2026-06-01",
    last_used=None,
    uses=0,
    skill_md_chars=3210,
    support_files=("references/env.md", "scripts/check.sh"),
)
_CANDIDATE_TEXT = (
    "- deploy-vercel\n"
    "  Description: Deploy the web app to Vercel.\n"
    "  Created by: a background reflection on a conversation\n"
    "  Created: 2026-05-01\n"
    "  Last changed: 2026-06-01\n"
    "  Last used: never\n"
    "  SKILL.md: 3210 characters\n"
    "  Support files: references/env.md, scripts/check.sh"
)


def test_librarian_brief_fills_its_placeholders_and_lists_each_candidate(tmp_path: Path) -> None:
    storage = StorageManager(data_dir=tmp_path / "data")
    storage.ensure_directories()
    (storage.prompts_dir / "librarian.md").write_text(
        "At most {tool_call_limit} calls; SKILL.md over {max_chars}."
        "\n\n{generated:candidates}\n\nEnd.\n",
        encoding="utf-8",
    )
    used = replace(_CANDIDATE, name="deploy-netlify", last_used="2026-09-20", uses=1)
    used_text = _CANDIDATE_TEXT.replace("deploy-vercel", "deploy-netlify").replace(
        "never", "2026-09-20 (1 use)"
    )

    assert librarian_brief(storage, [_CANDIDATE, used], limit=60) == (
        f"At most 60 calls; SKILL.md over 12000.\n\n{_CANDIDATE_TEXT}\n{used_text}\n\nEnd."
    )
    # The bundled brief carries no unfilled placeholder.
    bundled = librarian_brief(StorageManager(data_dir=tmp_path / "bundled"), [_CANDIDATE], limit=7)
    assert "{" not in bundled and "at most 7 calls" in bundled
