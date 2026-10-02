"""Compare the WebUI's installed packages and package manifest with its npm lock.

npm records the tree it installed in ``node_modules/.package-lock.json`` and
trusts that record as the description of what ``node_modules`` holds. The commit
check refuses to run the WebUI checks on packages other than the locked ones,
and ``worktree.py create`` copies the primary checkout's installation only when
it holds exactly the tree the worktree's lock names. The commit check also
refuses a package manifest that the lock does not record, which ``npm ci``
would reject later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# npm records the tree it installed here (npm 7 and later).
INSTALLED_RECORD = ".package-lock.json"
# The dependency maps of package.json that npm copies into the lock's root entry.
DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _json_object(path: Path) -> dict[str, Any]:
    """Return the JSON object in *path*; raise OSError or ValueError otherwise."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} holds no JSON object")
    return document


def _packages(lock_file: Path) -> dict[str, dict[str, Any]]:
    """Return the ``packages`` map of an npm lock file; raise OSError or ValueError otherwise."""
    packages = _json_object(lock_file).get("packages")
    if not isinstance(packages, dict) or not all(isinstance(e, dict) for e in packages.values()):
        raise ValueError(f"{lock_file} holds no package map")
    return packages


def installed_differences(node_modules: Path, lock_file: Path) -> list[str]:
    """Return how the packages in *node_modules* differ from those *lock_file* locks.

    An empty list means every installed package equals its locked entry and every
    locked package is installed, apart from optional ones: npm skips optional
    packages for other platforms.
    """
    try:
        locked = _packages(lock_file)
    except OSError, ValueError:
        return [f"{lock_file.name} is missing or unreadable"]
    try:
        installed = _packages(node_modules / INSTALLED_RECORD)
    except OSError, ValueError:
        return [f"{node_modules.name}/{INSTALLED_RECORD} is missing: npm installed nothing here"]
    differences: list[str] = []
    for path in sorted((locked.keys() | installed.keys()) - {""}):  # "": the project itself
        name = path.removeprefix("node_modules/")
        if path not in installed:
            if locked[path].get("optional") is not True:
                differences.append(f"{name}: locked, not installed")
        elif path not in locked:
            differences.append(f"{name}: installed, not locked")
        elif installed[path] != locked[path]:
            version, locked_version = installed[path].get("version"), locked[path].get("version")
            differences.append(
                f"{name}: installed {version}, locked {locked_version}"
                if version != locked_version
                else f"{name}: installed entry differs from the locked one"
            )
    return differences


def _dependency_maps(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    maps = {field: document.get(field) for field in DEPENDENCY_FIELDS}
    return {field: dict(value) if isinstance(value, dict) else {} for field, value in maps.items()}


def manifest_differences(manifest: Path, lock_file: Path) -> list[str]:
    """Return how *lock_file*'s record of the dependencies in *manifest* differs from them.

    ``npm install`` copies the dependency maps of ``package.json`` into the lock's
    root entry; a manifest edited without it differs. npm leaves out a dependency
    that ``optionalDependencies`` lists as well.
    """
    try:
        wanted = _dependency_maps(_json_object(manifest))
    except OSError, ValueError:
        return [f"{manifest.name} is missing or unreadable"]
    try:
        recorded = _dependency_maps(_packages(lock_file).get("", {}))
    except OSError, ValueError:
        return [f"{lock_file.name} is missing or unreadable"]
    for name in wanted["optionalDependencies"]:
        wanted["dependencies"].pop(name, None)
    differences: list[str] = []
    for field in DEPENDENCY_FIELDS:
        for name in sorted(wanted[field].keys() | recorded[field].keys()):
            spec, locked_spec = wanted[field].get(name), recorded[field].get(name)
            if spec != locked_spec:
                differences.append(
                    f"{field} {name}: {manifest.name} {spec or 'none'}, "
                    f"{lock_file.name} {locked_spec or 'none'}"
                )
    return differences
