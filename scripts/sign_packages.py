"""Sign already built update archives without importing or executing their contents.

CI runs this on a fresh signing runner after package builds and smokes passed.
Only this process receives the private key; it imports no application or build
modules. The installer's existing public key is the publishing identity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

KEY_ENVIRONMENT = "VBOT_RELEASE_SIGNING_KEY"
ARCHIVES = {
    "windows": {
        f"vbot-windows-x86_64-{shape}.zip"
        for shape in ("server", "server-desktop", "desktop-client")
    },
    "linux": {f"vbot-linux-{arch}-server.zip" for arch in ("aarch64", "x86_64")},
}


def release_public_key(installer: Path | None = None) -> str:
    """Read the public identity already distributed by the Linux installer."""
    installer = installer or Path(__file__).with_name("install.sh")
    matches: list[str] = re.findall(
        r'^RELEASE_PUBLIC_KEY="([A-Za-z0-9+/]+={0,2})"$',
        installer.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if len(matches) != 1 or len(base64.b64decode(matches[0], validate=True)) != 32:
        raise ValueError("installer must declare one raw Ed25519 release public key")
    return matches[0]


def sign_packages(directory: Path, platform: str, *, public_key: str) -> None:
    """Sign the complete platform archive set as opaque bytes with the trusted key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    encoded = os.environ.get(KEY_ENVIRONMENT)
    if not encoded:
        raise ValueError(f"release signing requires {KEY_ENVIRONMENT}")
    try:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded, validate=True))
    except ValueError:
        raise ValueError("release signing key must be a base64 raw Ed25519 private key") from None
    if base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii") != public_key:
        raise ValueError("release signing key does not match the installer's public key")
    expected = ARCHIVES[platform]
    if {path.name for path in directory.glob("*.zip")} != expected:
        raise ValueError("release signing requires the complete platform archive set")
    archives = [directory / name for name in sorted(expected)]
    if any(path.is_symlink() or not path.is_file() for path in archives):
        raise ValueError("release archives must be ordinary files")
    for archive in archives:
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        archive.with_suffix(".zip.sig").write_text(
            base64.b64encode(key.sign(digest)).decode("ascii") + "\n", encoding="ascii"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--platform", choices=ARCHIVES)
    parser.add_argument("--public-key-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.public_key_only and (args.dir is None or args.platform is None):
        parser.error("--dir and --platform are required for signing")
    try:
        public_key = release_public_key()
        if args.public_key_only:
            print(public_key)
        else:
            sign_packages(args.dir, args.platform, public_key=public_key)
    except (OSError, ValueError) as exc:
        print(f"Release signing failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
