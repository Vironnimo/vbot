"""Field names that other agent harnesses use for a Tool's parameters."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping


def spelling(value: str) -> str:
    """Return a field name without case, "_", "-", or spaces."""
    return re.sub(r"[\s_-]+", "", value.casefold())


class SpellingAliases(Mapping[str, str]):
    """Field aliases that match regardless of case, "_", "-", or spaces."""

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


__all__ = ["SpellingAliases", "spelling"]
