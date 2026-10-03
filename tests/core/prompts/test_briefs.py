"""Learning-Run briefs: assembly from shared prompt fragments."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.agents import LIBRARIAN_TOOLS
from core.prompts.briefs import (
    BRIEF_FRAGMENT_NAMES,
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

    # Groups are paragraphs; list fragments continue their lead line by line.
    signals_end = "[review-signals-end.md]"
    skill_part = (
        "[skill-shape.md]\n\n[skill-ladder.md]\n\n[skill-read-current.md] [review-skill-limits.md]"
    )
    assert reflection_brief(storage, "memory") == (
        "[review-intro-memory.md]"
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
    # A Librarian copy without its markers still names the Agent and lists the candidates.
    assert librarian_brief(storage, [_CANDIDATE], agent_id="coder", agent_name="Coder") == (
        f"You maintain the Skills of the Agent Coder (id coder).\n\n[librarian.md]"
        f"\n\n{_CANDIDATE_TEXT}"
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
        "The Skills of {generated:agent}; SKILL.md over {max_chars}."
        "\n\n{generated:candidates}\n\nEnd.\n",
        encoding="utf-8",
    )
    used = replace(_CANDIDATE, name="deploy-netlify", last_used="2026-09-20", uses=1)
    used_text = _CANDIDATE_TEXT.replace("deploy-vercel", "deploy-netlify").replace(
        "never", "2026-09-20 (1 use)"
    )

    # An Agent named like its id is named once.
    assert librarian_brief(storage, [_CANDIDATE, used], agent_id="coder", agent_name="coder") == (
        f"The Skills of coder; SKILL.md over 12000.\n\n{_CANDIDATE_TEXT}\n{used_text}\n\nEnd."
    )
    # The bundled brief carries no unfilled placeholder, names the Agent, and names
    # exactly the Librarian's Tools: its other backticked identifier is the
    # skill_manage parameter the merge steps teach.
    bundled = librarian_brief(
        StorageManager(data_dir=tmp_path / "bundled"),
        [_CANDIDATE],
        agent_id="coder",
        agent_name="Coder",
    )
    assert "{" not in bundled
    assert "the Skills of the Agent Coder (id coder)" in bundled
    identifiers = {token for token in bundled.split("`")[1::2] if token.isidentifier()}
    assert identifiers == {*LIBRARIAN_TOOLS, "absorbed_into"}
