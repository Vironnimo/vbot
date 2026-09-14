"""Provision the pinned native search dependency during installation or update."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx

from core.tools._search_binary import RESOURCE_ROOT, binary_spec, target_platform
from core.utils.processes import subprocess_creation_flags


def provision_search_runtime(root: Path = RESOURCE_ROOT, *, target: str | None = None) -> Path:
    """Atomically install a digest-locked upstream binary, reusing a verified copy."""
    selected = target or target_platform()
    destination, artifact = binary_spec(root, selected)
    if destination.is_file() and not destination.is_symlink():
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == artifact["binary_sha256"]:
                return destination
    with (
        httpx.Client(follow_redirects=True, trust_env=False, timeout=60) as client,
        client.stream("GET", artifact["url"]) as response,
    ):
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("The search engine archive exceeds its allowed size.")
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ValueError("The search engine archive failed integrity verification.")
    name = destination.name
    if artifact["url"].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            candidates = [item for item in archive.infolist() if item.filename.endswith("/" + name)]
            if len(candidates) != 1 or candidates[0].file_size > 32 * 1024 * 1024:
                raise ValueError("Invalid search engine archive layout.")
            executable = archive.read(candidates[0])
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = [item for item in archive.getmembers() if item.name.endswith("/" + name)]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 32 * 1024 * 1024:
                raise ValueError("Invalid search engine archive layout.")
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise ValueError("Search executable missing from archive.")
            with extracted:
                executable = extracted.read()
    if hashlib.sha256(executable).hexdigest() != artifact["binary_sha256"]:
        raise ValueError("The extracted search engine failed integrity verification.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".ripgrep-", suffix=destination.suffix, dir=destination.parent
    )
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(executable)
            output.flush()
            os.fsync(output.fileno())
        staging.chmod(0o755)
        if selected == target_platform():
            probe = subprocess.run(
                [str(staging), "--pcre2-version"],
                capture_output=True,
                timeout=15,
                creationflags=subprocess_creation_flags(),
                check=False,
            )
            if probe.returncode:
                raise ValueError("The pinned search engine cannot run with PCRE2 on this host.")
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=RESOURCE_ROOT)
    parser.add_argument("--target")
    args = parser.parse_args()
    provision_search_runtime(args.root, target=args.target)
    print("Search engine verified and ready.")


if __name__ == "__main__":
    main()
