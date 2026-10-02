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

_COMBINED_INTRO = "review-intro-combined.md"
_MEMORY_INTRO = "review-intro-memory.md"
_SKILL_INTRO = "review-intro-skill.md"
_SIGNALS_LEAD = "review-signals-lead.md"
_SKILL_SIGNALS = "review-signals-skill.md"
_MEMORY_SIGNALS = "review-signals-memory.md"
_SIGNALS_END = "review-signals-end.md"
_ROUTING = "learning-routing.md"
_MEMORY_ONLY_ROUTING = "review-routing-memory-only.md"
_SKILL_ONLY_ROUTING = "review-routing-skill-only.md"
_SKIP = "review-skip.md"
_MEMORY_METHOD = "review-memory.md"
_SKILL_SHAPE = "skill-shape.md"
_SKILL_LADDER = "skill-ladder.md"
_SKILL_READ = "skill-read-current.md"
_SKILL_LIMITS = "review-skill-limits.md"
_CLOSING = "review-closing.md"
_LEARN_INTRO = "learn-intro.md"
_LEARN_METHOD = "learn-method.md"
_LIBRARIAN = "librarian.md"

# A recipe is a sequence of groups; groups are joined by one blank line. A group
# joins its fragments with its joiner: ``_PROSE`` continues a paragraph (one
# space), ``_LINES`` continues a list (one newline). A fragment is whole sentences
# or whole list items and may hold several paragraphs.
_PROSE = " "
_LINES = "\n"
_Group = tuple[str, tuple[str, ...]]
_Recipe = tuple[_Group, ...]


def _signals(*lists: str) -> _Group:
    return (_LINES, (_SIGNALS_LEAD, *lists))


_SKILL_PART: tuple[_Group, ...] = (
    (_PROSE, (_SKILL_SHAPE,)),
    (_PROSE, (_SKILL_LADDER,)),
    (_PROSE, (_SKILL_READ, _SKILL_LIMITS)),
)
_REFLECTION_RECIPES: dict[ReflectionScope, _Recipe] = {
    "memory": (
        (_PROSE, (_MEMORY_INTRO,)),
        _signals(_MEMORY_SIGNALS),
        (_PROSE, (_SIGNALS_END,)),
        (_PROSE, (_ROUTING, _MEMORY_ONLY_ROUTING)),
        (_PROSE, (_SKIP,)),
        (_PROSE, (_MEMORY_METHOD,)),
        (_PROSE, (_CLOSING,)),
    ),
    "skill": (
        (_PROSE, (_SKILL_INTRO,)),
        _signals(_SKILL_SIGNALS),
        (_PROSE, (_SIGNALS_END,)),
        (_PROSE, (_ROUTING, _SKILL_ONLY_ROUTING)),
        (_PROSE, (_SKIP,)),
        *_SKILL_PART,
        (_PROSE, (_CLOSING,)),
    ),
    "combined": (
        (_PROSE, (_COMBINED_INTRO,)),
        _signals(_SKILL_SIGNALS, _MEMORY_SIGNALS),
        (_PROSE, (_SIGNALS_END,)),
        (_PROSE, (_ROUTING,)),
        (_PROSE, (_SKIP,)),
        (_PROSE, (_MEMORY_METHOD,)),
        *_SKILL_PART,
        (_PROSE, (_CLOSING,)),
    ),
}
_LEARN_RECIPE: _Recipe = (
    (_PROSE, (_LEARN_INTRO,)),
    (_PROSE, (_SKILL_SHAPE,)),
    (_PROSE, (_SKILL_LADDER,)),
    (_PROSE, (_SKILL_READ,)),
    (_PROSE, (_LEARN_METHOD,)),
)

REVIEW_TOOL_CALL_LIMIT = 16
"""Tool calls one review Run can dispatch; its brief states the same number."""
_TOOL_CALL_LIMIT_MARK = "{tool_call_limit}"

BRIEF_FRAGMENT_NAMES: frozenset[str] = frozenset(
    {
        *(
            name
            for recipe in (*_REFLECTION_RECIPES.values(), _LEARN_RECIPE)
            for _joiner, group in recipe
            for name in group
        ),
        _LIBRARIAN,
    }
)
"""Every prompt fragment a brief reads; Storage must allowlist and bundle each."""

# The Librarian brief's placeholders. The candidate list is rendered here; a
# fragment copy without its marker gets the list appended as a last paragraph.
_LIBRARIAN_MAX_CHARS = "{max_chars}"
_LIBRARIAN_CANDIDATES = "{generated:candidates}"
# A SKILL.md longer than about this many characters is hard to use.
LIBRARIAN_SKILL_MD_MAX_CHARS = 12000
# Who created a candidate, in the words of the Agent the brief addresses.
_LIBRARIAN_ORIGIN_TEXTS = {
    "human": "the user",
    "agent": "you, during a conversation",
    "reflection": "a background reflection on a conversation",
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
    it.
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
    brief = _assemble(fragments, _REFLECTION_RECIPES[scope]).replace(
        _TOOL_CALL_LIMIT_MARK, str(REVIEW_TOOL_CALL_LIMIT)
    )
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
        .replace(_TOOL_CALL_LIMIT_MARK, str(limit))
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
        )
    )


def _assemble(fragments: BriefFragmentReader, recipe: _Recipe) -> str:
    return "\n\n".join(
        joiner.join(fragments.read_prompt_fragment(name).strip() for name in group)
        for joiner, group in recipe
    )
