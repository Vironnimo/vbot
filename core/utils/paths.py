"""Filesystem-path presentation at Model-facing boundaries.

Runtime domains keep native :class:`pathlib.Path` values for filesystem work.
Only the producers that knowingly expose one of those paths to a Model call
``model_path`` so separators round-trip safely through JSON Tool Calls without
rewriting arbitrary text, commands, URLs, or file content.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath


def model_path(path: str | os.PathLike[str]) -> str:
    """Render a filesystem path with forward-slash separators.

    The representation changes separators only. Relative paths stay relative,
    absolute paths stay absolute, and no resolution, case folding, existence
    check, or host-flavor conversion is performed.
    """

    if isinstance(path, PurePath):
        return path.as_posix()
    return Path(path).as_posix()


__all__ = ["model_path"]
