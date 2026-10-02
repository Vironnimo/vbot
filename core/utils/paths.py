"""Filesystem paths at Model-facing boundaries: presentation and ``file:`` URLs.

Runtime domains keep native :class:`pathlib.Path` values for filesystem work.
Only the producers that knowingly expose one of those paths to a Model call
``model_path`` so separators round-trip safely through JSON Tool Calls without
rewriting arbitrary text, commands, URLs, or file content. Tools that accept a
``file:`` URL in place of a path read it with ``file_url_path``.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname


def model_path(path: str | os.PathLike[str]) -> str:
    """Render a filesystem path with forward-slash separators.

    The representation changes separators only. Relative paths stay relative,
    absolute paths stay absolute, and no resolution, case folding, existence
    check, or host-flavor conversion is performed.
    """

    if isinstance(path, PurePath):
        return path.as_posix()
    return Path(path).as_posix()


def file_url_path(url: str) -> str | None:
    """Return the path on this computer that the ``file:`` URL ``url`` names.

    Percent escapes are decoded; a query or fragment is dropped. A host stays part
    of the path where the platform can name one: on Windows
    ``file://server/share/a.txt`` is the UNC path to ``a.txt`` on that share, and
    ``file:///C:/a.txt`` keeps its drive. Elsewhere a URL whose host is neither
    empty, ``localhost`` nor this computer's name names a file on another
    computer: ``None``. Host names compare case-insensitively, so
    ``file://LOCALHOST/...`` is local as well.
    """
    parts = urlsplit(url)
    if parts.netloc.lower() == "localhost":
        # url2pathname only recognises the lowercase spelling.
        url = urlunsplit(parts._replace(netloc="localhost"))
    try:
        return url2pathname(url, require_scheme=True)
    except URLError:
        return None


__all__ = ["file_url_path", "model_path"]
