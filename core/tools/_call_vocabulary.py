"""Owner-side vocabulary for reading Model call syntax: spellings and placeholders.

Tool owners use these helpers inside their ``argument_normalizer`` to accept
the field names other harnesses use and to recognize values a Model writes into
optional fields it does not mean to use. Nothing here decides what a value
means for a Tool; each owner selects the fields and words it accepts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any


def spelling(value: str) -> str:
    """Return ``value`` without case, spaces, and punctuation: ``Agent-ID`` -> ``agentid``."""
    return re.sub(r"[\W_]+", "", value.casefold())


class SpellingAliases(Mapping[str, str]):
    """Field aliases that match regardless of case, spaces, and punctuation."""

    def __init__(self, fields: Mapping[str, Iterable[str]]) -> None:
        self._aliases = {
            spelling(alias): field for field, names in fields.items() for alias in names
        }

    def __getitem__(self, key: str) -> str:
        return self._aliases[spelling(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._aliases)

    def __len__(self) -> int:
        return len(self._aliases)


# Words Models write into optional fields to mean "not used", compared by spelling.
PLACEHOLDER_WORDS = frozenset(
    {
        "blank",
        "empty",
        "invalidplaceholder",
        "na",
        "nil",
        "none",
        "notapplicable",
        "notset",
        "null",
        "omit",
        "omitted",
        "placeholder",
        "tbd",
        "undefined",
        "unset",
        "unused",
    }
)


def is_placeholder(value: Any, words: Iterable[str] = PLACEHOLDER_WORDS) -> bool:
    """Return whether ``value`` stands for an omitted optional field.

    ``None``, blank or punctuation-only text (``" "``, ``"."``, ``"???"``), and
    the given placeholder words (``"unused"``, ``"<none>"``, ``"__omit__"``)
    qualify. Any other text, including an unknown id, is a real value.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = spelling(value)
    return not text or text in words


__all__ = ["PLACEHOLDER_WORDS", "SpellingAliases", "is_placeholder", "spelling"]
