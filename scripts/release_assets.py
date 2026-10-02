"""Check that a set of release assets is complete before or after publication.

A complete release carries, for every platform and shape vBot ships, the signed
update package, its signature and its runtime inventory; Windows shapes also
carry their installer. ``vbot-release.json`` names the published version; the
publishers add the SHA-256 digest of every other asset to it with
``--record-digests``, which the Installers verify their downloads against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

WINDOWS_SHAPES = ("server", "server-desktop", "desktop-client")
LINUX_PLATFORMS = ("linux-aarch64", "linux-x86_64")
IDENTITY = "vbot-release.json"
#: Installed updaters refuse a larger identity (``cli/application/packages.py``
#: ``MAX_IDENTITY_BYTES``), so the published one, digests included, stays within it.
IDENTITY_LIMIT = 4096


def expected_assets(version: str) -> set[str]:
    packages = [("windows-x86_64", shape) for shape in WINDOWS_SHAPES]
    packages += [(platform, "server") for platform in LINUX_PLATFORMS]
    names = {IDENTITY}
    for platform, shape in packages:
        archive = f"vbot-{platform}-{shape}.zip"
        names |= {archive, f"{archive}.sig", f"vbot-{platform}-{shape}-runtime-inventory.json"}
    names |= {f"vBot-{version}-windows-x86_64-{shape}.exe" for shape in WINDOWS_SHAPES}
    return names


def problems(
    names: Iterable[str],
    version: str,
    *,
    identity: dict[str, object] | None = None,
    revision: str | None = None,
    channel: str | None = None,
) -> list[str]:
    found = set(names)
    result = [f"missing asset {name}" for name in sorted(expected_assets(version) - found)]
    if identity is not None:
        wanted = {"version": version, "revision": revision, "channel": channel}
        for key, value in wanted.items():
            if value is not None and identity.get(key) != value:
                result.append(f"{IDENTITY} names {key} {identity.get(key)!r}, expected {value!r}")
    return result


def record_digests(directory: Path) -> str | None:
    """Add the digest of every other file in ``directory`` to its identity.

    Returns the problem instead of writing an identity larger than updaters accept.
    """
    path = directory / IDENTITY
    identity = json.loads(path.read_text(encoding="utf-8"))
    digests: dict[str, str] = {}
    for item in sorted(directory.iterdir()):
        if item.is_file() and item.name != IDENTITY:
            with item.open("rb") as handle:
                digests[item.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    identity["assets"] = digests
    content = (json.dumps(identity, separators=(",", ":")) + "\n").encode("utf-8")
    if len(content) > IDENTITY_LIMIT:
        return (
            f"{IDENTITY} with the digests of {len(digests)} assets has {len(content)} bytes; "
            f"installed updaters accept at most {IDENTITY_LIMIT}"
        )
    path.write_bytes(content)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dir", type=Path, help="Directory holding the assets to publish")
    source.add_argument("--names", type=Path, help="File listing published asset names")
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision")
    parser.add_argument("--channel", choices=("release", "main"))
    parser.add_argument(
        "--record-digests",
        action="store_true",
        help=f"Once the check passes, add every other asset's SHA-256 digest to the {IDENTITY} "
        "in --dir",
    )
    args = parser.parse_args(argv)
    if args.record_digests and args.dir is None:
        parser.error("--record-digests needs --dir")
    identity = None
    if args.dir is not None:
        names = [path.name for path in args.dir.iterdir() if path.is_file() and path.stat().st_size]
        if (args.dir / IDENTITY).is_file():
            identity = json.loads((args.dir / IDENTITY).read_text(encoding="utf-8"))
    else:
        names = args.names.read_text(encoding="utf-8").split()
    found = problems(
        names, args.version, identity=identity, revision=args.revision, channel=args.channel
    )
    if not found and args.record_digests:
        failure = record_digests(args.dir)
        found = [failure] if failure else []
    for problem in found:
        print(f"Release assets: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
