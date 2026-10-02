"""Directory links that tests can create without privileges on every platform."""

from __future__ import annotations

import sys
from pathlib import Path


def link_directory(link: Path, target: Path) -> None:
    """Create ``link`` pointing at the directory ``target``.

    Windows gets a junction, which needs no privileges and which ``os.walk``,
    ``Path.rglob`` and ``shutil.copytree`` enter, unlike a symbolic link. Other
    platforms get a directory symbolic link.
    """
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
