"""One-shot briefs for internal learning Runs, assembled from shared fragments.

A brief is the instruction an internal Run receives as its input: the three
Reflection review scopes, ``/learn`` and a Librarian pass. Text the briefs
share lives in one fragment file, so the scopes cannot drift apart. Storage
resolves each fragment on its own (a hand-created ``<data_dir>/prompts/<name>``
copy overrides the bundled resource), and this module owns how fragments
compose into a brief and how a brief's variable parts are rendered.

Assembly reads fragment files and blocks: call it off the Event Loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

ReflectionScope = Literal["memory", "skill", "combined"]

_MEMORY_INTRO = "reflect-memory-intro.md"
_SKILL_INTRO = "reflect-skill-intro.md"
_COMBINED_INTRO = "reflect-combined-intro.md"
_COMBINED_SKILL_LEAD = "reflect-combined-skill-lead.md"
_MEMORY_METHOD = "reflect-memory-method.md"
_SKILL_OWNERSHIP_CHECK = "skill-ownership-check.md"
_SKILL_METHOD = "reflect-skill-method.md"
_MEMORY_CLOSING = "reflect-memory-closing.md"
_SKILL_CLOSING = "reflect-skill-closing.md"
_COMBINED_CLOSING = "reflect-combined-closing.md"
_LEARN_INTRO = "learn-intro.md"
_LEARN_METHOD = "learn-method.md"
_LIBRARIAN = "librarian.md"

# A recipe is a sequence of paragraph groups. The fragments of one group continue
# a paragraph (joined by one space); each group starts a new paragraph (joined by
# one blank line). A fragment is whole sentences and may hold several paragraphs.
_Recipe = tuple[tuple[str, ...], ...]
_REFLECTION_RECIPES: dict[ReflectionScope, _Recipe] = {
    "memory": (
        (_MEMORY_INTRO, _MEMORY_METHOD),
        (_MEMORY_CLOSING,),
    ),
    "skill": (
        (_SKILL_INTRO, _SKILL_OWNERSHIP_CHECK, _SKILL_METHOD),
        (_SKILL_CLOSING,),
    ),
    "combined": (
        (_COMBINED_INTRO, _MEMORY_METHOD),
        (_COMBINED_SKILL_LEAD, _SKILL_OWNERSHIP_CHECK, _SKILL_METHOD),
        (_COMBINED_CLOSING,),
    ),
}
_LEARN_RECIPE: _Recipe = ((_LEARN_INTRO, _SKILL_OWNERSHIP_CHECK, _LEARN_METHOD),)

BRIEF_FRAGMENT_NAMES: frozenset[str] = frozenset(
    {
        *(
            name
            for recipe in (*_REFLECTION_RECIPES.values(), _LEARN_RECIPE)
            for group in recipe
            for name in group
        ),
        _LIBRARIAN,
    }
)
"""Every prompt fragment a brief reads; Storage must allowlist and bundle each."""

# The Librarian brief's placeholders. The candidate list is rendered here; a
# fragment copy without its marker gets the list appended as a last paragraph.
_LIBRARIAN_LIMIT = "{limit}"
_LIBRARIAN_MAX_CHARS = "{max_chars}"
_LIBRARIAN_CANDIDATES = "{generated:candidates}"
# A SKILL.md longer than about this many characters is hard to use.
LIBRARIAN_SKILL_MD_MAX_CHARS = 12000
# Who created a candidate, in the words of the Agent the brief addresses.
_LIBRARIAN_ORIGIN_TEXTS = {
    "agent": "you, during a conversation",
    "reflection": "a background review of a conversation",
    "librarian": "an earlier Librarian pass",
}

REFLECTION_FOCUS_TEMPLATE = "The user asked you to focus this reflection on:\n{focus}"
LEARN_REQUEST_TEMPLATE = "The request to learn from:\n{request}"
LEARN_WITHOUT_REQUEST = (
    "No request was given. If the recent conversation clearly establishes reusable "
    "learning, apply the instructions above to it. Otherwise, ask the user what "
    "they want captured."
)


@dataclass(frozen=True)
class LibrarianCandidate:
    """One Skill a Librarian pass may change, with the facts its brief lists.

    Dates are ISO dates (``YYYY-MM-DD``). ``changed`` is the last change of
    the Skill's files, ``last_used`` its last use in a conversation (``None``
    when it was never used there) and ``uses`` the number of Sessions that used
    it. ``scheduled`` is set when a schedule's instructions name the Skill.
    """

    name: str
    description: str
    origin: str
    created: str
    changed: str
    last_used: str | None
    uses: int
    skill_md_chars: int
    support_files: tuple[str, ...]
    scheduled: bool


class BriefFragmentReader(Protocol):
    """Prompt fragment lookup by allowlisted resource name (Storage satisfies it)."""

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Return the effective text of one prompt fragment."""
        ...


def reflection_brief(
    fragments: BriefFragmentReader,
    scope: ReflectionScope,
    *,
    focus: str | None = None,
) -> str:
    """Return the review brief for ``scope``, with an optional user focus appended."""
    brief = _assemble(fragments, _REFLECTION_RECIPES[scope])
    cleaned = (focus or "").strip()
    if not cleaned:
        return brief
    return f"{brief}\n\n{REFLECTION_FOCUS_TEMPLATE.format(focus=cleaned)}"


def learn_brief(fragments: BriefFragmentReader, request: str | None) -> str:
    """Return the ``/learn`` Skill-authoring brief for the user's request."""
    brief = _assemble(fragments, _LEARN_RECIPE)
    cleaned = (request or "").strip()
    if not cleaned:
        return f"{brief}\n\n{LEARN_WITHOUT_REQUEST}"
    return f"{brief}\n\n{LEARN_REQUEST_TEMPLATE.format(request=cleaned)}"


def librarian_brief(
    fragments: BriefFragmentReader,
    candidates: Sequence[LibrarianCandidate],
    *,
    limit: int,
) -> str:
    """Return the Librarian brief listing ``candidates``, with ``limit`` Tool calls."""
    brief = (
        fragments.read_prompt_fragment(_LIBRARIAN)
        .strip()
        .replace(_LIBRARIAN_LIMIT, str(limit))
        .replace(_LIBRARIAN_MAX_CHARS, str(LIBRARIAN_SKILL_MD_MAX_CHARS))
    )
    listed = "\n".join(_candidate_text(candidate) for candidate in candidates)
    if _LIBRARIAN_CANDIDATES in brief:
        return brief.replace(_LIBRARIAN_CANDIDATES, listed)
    return f"{brief}\n\n{listed}"


def _candidate_text(candidate: LibrarianCandidate) -> str:
    description = " ".join(candidate.description.split()) or "(none)"
    origin = _LIBRARIAN_ORIGIN_TEXTS.get(candidate.origin, candidate.origin)
    uses = "1 use" if candidate.uses == 1 else f"{candidate.uses} uses"
    used = "never" if candidate.last_used is None else f"{candidate.last_used} ({uses})"
    files = ", ".join(candidate.support_files) or "none"
    return "\n".join(
        (
            f"- {candidate.name}",
            f"  Description: {description}",
            f"  Created by: {origin}",
            f"  Created: {candidate.created}",
            f"  Last changed: {candidate.changed}",
            f"  Last used: {used}",
            f"  SKILL.md: {candidate.skill_md_chars} characters",
            f"  Support files: {files}",
            f"  Used by a schedule: {'yes' if candidate.scheduled else 'no'}",
        )
    )


def _assemble(fragments: BriefFragmentReader, recipe: _Recipe) -> str:
    return "\n\n".join(
        " ".join(fragments.read_prompt_fragment(name).strip() for name in group) for group in recipe
    )
