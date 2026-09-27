"""Build a checkout's WebUI and Extension pages, reusing installed npm packages.

The source-checkout updater and the Windows application's source updates both
build from a checkout's ``webui`` directory.  ``npm ci`` dominates that build, so
it runs only when what it installs from changed: ``package-lock.json``,
``package.json`` or the Node.js version.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

_LOGGER = logging.getLogger("vbot.webui_build")

# Written into node_modules after a successful ``npm ci``; ``npm ci`` deletes it again.
_PACKAGES_MARKER = ".vbot-npm-ci"
_PACKAGE_FILES = ("package-lock.json", "package.json")


def build_webui(
    webui: Path,
    *,
    node_version: str | None,
    npm: Callable[[list[str]], None],
) -> None:
    """Install the npm packages when their inputs changed, then run ``npm run build``.

    ``npm(args)`` runs npm with ``args`` in ``webui`` and raises on failure.
    ``node_version`` is the output of ``node --version``; ``None`` disables reuse.
    ``npm ci`` deletes ``node_modules`` first, which also removes the marker of the
    previous install, so an interrupted install is never reused.  A build that
    fails with reused packages is retried once after a clean install, because
    those packages may have been changed outside npm.
    """

    identity = _packages_identity(webui, node_version)
    marker = webui / "node_modules" / _PACKAGES_MARKER

    def install_packages() -> None:
        npm(["ci"])
        if identity is None:
            return
        try:
            marker.write_text(identity, encoding="ascii")
        except OSError as exc:
            # Only reuse is lost: the next build installs the packages again.
            _LOGGER.warning("Could not record the installed WebUI packages: %s", exc)

    try:
        reused = identity is not None and marker.read_text(encoding="ascii") == identity
    except (OSError, UnicodeError):
        reused = False
    if not reused:
        install_packages()
    try:
        npm(["run", "build"])
    except Exception:
        if not reused:
            raise
        install_packages()
        npm(["run", "build"])


def _packages_identity(webui: Path, node_version: str | None) -> str | None:
    """Fingerprint what ``npm ci`` installs from: the lock, the manifest and Node itself."""

    if not node_version:
        return None
    try:
        manifests = [(webui / name).read_bytes() for name in _PACKAGE_FILES]
    except OSError:
        return None
    value = hashlib.sha256(b"vbot-npm-ci-1\0" + node_version.encode() + b"\0")
    for contents in manifests:
        value.update(hashlib.sha256(contents).digest())
    return value.hexdigest()
