"""Field-alias tables that ignore how an Agent spells a parameter name."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping


def spelling(value: str) -> str:
    """Return the spelling-insensitive key: casefolded, without "_", "-" or spaces."""
    return re.sub(r"[\s_-]+", "", value.casefold())


class SpellingAliases(Mapping[str, str]):
    """Map alias names to canonical fields regardless of case, "_", "-" or spaces.

    Pass it as ``field_aliases`` to ``normalize_call_arguments`` so ``maxChars``,
    ``max_chars`` and ``Max-Chars`` all resolve through one listed alias.
    """

    def __init__(self, fields: dict[str, tuple[str, ...]]) -> None:
        self._aliases = {
            spelling(alias): field for field, names in fields.items() for alias in names
        }

    def __getitem__(self, key: str) -> str:
        return self._aliases[spelling(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._aliases)

    def __len__(self) -> int:
        return len(self._aliases)
