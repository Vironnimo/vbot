"""Directory links and link look-alikes that tests can use on every platform."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# IO_REPARSE_TAG_CLOUD_6, the tag of OneDrive Files On-Demand placeholders.
_CLOUD_PLACEHOLDER_TAG = 0x9000601A


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


def cloud_placeholder_status(status: os.stat_result) -> Any:
    """Return ``status`` as Windows reports it for a cloud-file placeholder.

    A placeholder (OneDrive Files On-Demand) is a reparse point whose tag names no
    link target, so it is an ordinary file. Real placeholders need a sync provider;
    tests substitute this unfollowed status for one entry instead.
    """
    fields = {name: getattr(status, name) for name in dir(status) if name.startswith("st_")}
    fields["st_file_attributes"] = (
        fields.get("st_file_attributes", 0) | stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    fields["st_reparse_tag"] = _CLOUD_PLACEHOLDER_TAG
    return SimpleNamespace(**fields)
