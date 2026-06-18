"""Project cwd normalization, identity keys, and the duplicate-cwd check.

Deployment is Linux, development is Windows (see PROJECT.md → Context), so cwd
handling is **both-flavored and explicit** rather than relying on host behavior:

- A *canonical cwd key* is what duplicate detection compares. It resolves
  symlinks (``os.path.realpath``), drops a trailing separator, and collapses
  ``.``/``..`` segments. Case is folded **only on Windows** (NTFS is
  case-insensitive); POSIX paths stay case-sensitive (``/srv/A`` and ``/srv/a``
  are two different repos). Path separators are normalized so a Windows path
  reached with ``/`` or ``\\`` produces one key.
- The *stored cwd* keeps the user's resolved absolute path (case preserved) for
  display and for tool path resolution; only the comparison key is folded.

The slug helpers turn an arbitrary display name into a filesystem-safe
``project_id`` per the canonical project-id rule (``PROJECT_ID_PATTERN``).
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from core.settings import PROJECT_ID_PATTERN

# Longest project-id (and slug) length, matching the canonical PROJECT_ID_PATTERN
# (leading char + up to 63 more).
MAX_PROJECT_ID_LENGTH = 64
# Characters that are not allowed inside a slug get collapsed into this joiner.
# Letters/digits/underscore/hyphen are kept (matching PROJECT_ID_PATTERN); any
# run of other characters collapses to a single hyphen.
_SLUG_SEPARATOR = "-"
_SLUG_INVALID_PATTERN = re.compile(r"[^a-z0-9_-]+")
_SLUG_EDGE_PATTERN = re.compile(r"^[-_]+|[-_]+$")


def normalize_cwd(cwd: str | os.PathLike[str]) -> Path:
    """Return the stored, display-facing absolute cwd: resolved, case preserved.

    Symlinks are resolved and ``.``/``..`` collapsed so the stored path is the
    real location of the repo. Case is **not** folded here — the stored value is
    what tools resolve against and what the user sees. Use :func:`cwd_identity_key`
    for duplicate comparison.
    """
    raw = str(cwd).strip()
    if not raw:
        raise ValueError("cwd must be a non-empty path")
    # realpath resolves symlinks and normalizes separators/`.`/`..` on both
    # flavors; Path keeps the result first-class for callers.
    return Path(os.path.realpath(Path(raw).expanduser()))


def cwd_identity_key(cwd: str | os.PathLike[str]) -> str:
    """Return the comparison key that decides whether two cwds are the same repo.

    Two cwds collide when their keys are equal. The key resolves symlinks, drops
    a trailing separator, and — **only on Windows** — case-folds, because the
    Windows filesystem is case-insensitive while POSIX is not.
    """
    resolved = str(normalize_cwd(cwd))
    # realpath already stripped a trailing separator and collapsed `.`/`..`;
    # fold case on Windows only.
    if os.name == "nt":
        return resolved.casefold()
    return resolved


def cwd_exists(cwd: str | os.PathLike[str]) -> bool:
    """Return whether the cwd currently resolves to an existing directory.

    A project key is the stable ``project_id``, not the path, so a repo that was
    moved or deleted is detectable (``False``) without losing the project — the
    accessor offers a re-point.
    """
    try:
        resolved = normalize_cwd(cwd)
    except ValueError:
        return False
    return resolved.is_dir()


def slugify_project_id(display_name: str) -> str:
    """Derive a filesystem-safe ``project_id`` slug from a display name.

    lowercase → transliterate/strip Unicode → non-alphanumeric runs become a
    single hyphen → trim leading digit-leading is fine (the rule allows it) but
    leading/trailing separators are trimmed → truncate to 64 chars. Raises
    :class:`ValueError` when nothing slug-worthy remains (caller surfaces it as a
    "not slugifiable" scan/report finding).
    """
    if not isinstance(display_name, str):
        raise ValueError("display_name must be a string")

    # Decompose accents then drop non-ASCII so transliteration is deterministic
    # across hosts (no locale dependence).
    decomposed = unicodedata.normalize("NFKD", display_name)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    collapsed = _SLUG_INVALID_PATTERN.sub(_SLUG_SEPARATOR, ascii_text)
    trimmed = _SLUG_EDGE_PATTERN.sub("", collapsed)
    slug = trimmed[:MAX_PROJECT_ID_LENGTH]
    slug = _SLUG_EDGE_PATTERN.sub("", slug)
    if not slug or PROJECT_ID_PATTERN.fullmatch(slug) is None:
        raise ValueError(f"display name cannot be slugified into a project id: {display_name!r}")
    return slug
