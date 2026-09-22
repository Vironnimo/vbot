"""Resolve Skill sources to complete packages, independently of any one catalog."""

from __future__ import annotations

import json
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from core.skills._packages import (
    MAX_DOWNLOAD_BYTES,
    PackageError,
    PackageFile,
    is_redirect,
    package_path,
    read_archive,
    read_directory,
    unwrap_archive,
)


@dataclass(frozen=True)
class SkillSource:
    files: dict[str, PackageFile]
    source: str
    fallback_name: str
    path: str | None = None
    name: str | None = None


def _url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.username or url.password:
            raise ValueError
        return url
    except (httpx.InvalidURL, ValueError) as error:
        raise PackageError("Use an HTTP(S) URL without embedded credentials.") from error


def _download(url: str, *, limit: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Bound redirects, time and decoded bytes; never forward ambient credentials."""
    current = _url(url)
    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), trust_env=False) as client:
            for _ in range(6):
                with client.stream(
                    "GET", current, headers={"User-Agent": "vBot-Skill-Installer"}
                ) as response:
                    if time.monotonic() - started > 120:
                        raise PackageError("Skill download exceeded its time limit.")
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PackageError("Skill download returned an empty redirect.")
                        next_url = _url(urljoin(str(current), location))
                        if current.scheme == "https" and next_url.scheme != "https":
                            raise PackageError("Skill download redirected from HTTPS to HTTP.")
                        current = next_url
                        continue
                    if response.status_code != 200:
                        raise PackageError(
                            f"Skill download failed (HTTP {response.status_code}). "
                            "Use a public download/repository URL or a server-local package."
                        )
                    length = response.headers.get("content-length", "")
                    # Compare decimal text so hostile headers cannot exceed
                    # Python's integer-conversion limit before error mapping.
                    declared = length.lstrip("0") or "0"
                    bound = str(limit)
                    if (
                        length.isascii()
                        and length.isdigit()
                        and (
                            len(declared) > len(bound)
                            or len(declared) == len(bound)
                            and declared > bound
                        )
                    ):
                        raise PackageError("Skill download exceeds its size limit.")
                    result = bytearray()
                    for chunk in response.iter_bytes():
                        if len(result) + len(chunk) > limit:
                            raise PackageError("Skill download exceeds its size limit.")
                        if time.monotonic() - started > 120:
                            raise PackageError("Skill download exceeded its time limit.")
                        result.extend(chunk)
                    return bytes(result)
        raise PackageError("Skill download exceeded its redirect limit.")
    except httpx.HTTPError as error:
        raise PackageError(
            f"Skill download failed ({type(error).__name__}); no package was installed."
        ) from error


def _json(url: str) -> dict:
    try:
        value = json.loads(_download(url, limit=1024 * 1024))
    except (ValueError, UnicodeDecodeError) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError("Skill source returned invalid metadata.") from error
    if not isinstance(value, dict):
        raise PackageError("Skill source returned invalid metadata.")
    return value


def _github(
    owner: str, repo: str, *, ref: str, path: str | None, name: str | None = None
) -> SkillSource:
    package_path(owner)
    package_path(repo)
    if "/" in owner or "/" in repo or not ref or any(ord(c) < 32 for c in ref):
        raise PackageError("Invalid GitHub repository or revision.")
    # Resolve a branch/tag once to an immutable commit before downloading the bundle.
    metadata = _json(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/commits/{quote(ref, safe='')}"
    )
    commit = metadata.get("sha")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(c not in "0123456789abcdef" for c in commit)
    ):
        raise PackageError("GitHub did not return a valid commit for the requested revision.")
    archive = _download(
        f"https://codeload.github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/zip/{commit}"
    )
    origin = f"https://github.com/{owner}/{repo}/tree/{commit}"
    return SkillSource(unwrap_archive(read_archive(archive)), origin, repo, path, name)


def _clawhub(owner: str, slug: str, version: str | None) -> SkillSource:
    owner = owner.lstrip("@")
    package_path(owner)
    package_path(slug)
    if "/" in owner or "/" in slug:
        raise PackageError("Invalid ClawHub publisher or Skill name.")
    publisher_query = urlencode({"ownerHandle": owner})
    endpoint = f"https://clawhub.ai/api/v1/skills/{quote(slug, safe='')}"
    details = _json(f"{endpoint}?{publisher_query}")
    publisher = details.get("owner")
    if (
        not isinstance(publisher, dict)
        or str(publisher.get("handle", "")).casefold() != owner.casefold()
    ):
        raise PackageError("ClawHub did not verify the publisher in the supplied link.")
    if version is None:
        # Use the public resolver: a catalog entry may host an archive or point to
        # a specific GitHub commit. Never bypass a blocked/pending resolver result.
        resolved = _json(f"{endpoint}/install?{publisher_query}")
        if resolved.get("ok") is not True or resolved.get("slug") != slug:
            raise PackageError("ClawHub did not return an installable Skill.")
        if resolved.get("installKind") == "github":
            github = resolved.get("github")
            if not isinstance(github, dict):
                raise PackageError("ClawHub returned an invalid GitHub source.")
            repo, commit, path = (github.get(key) for key in ("repo", "commit", "path"))
            if (
                not isinstance(repo, str)
                or len(repo.split("/")) != 2
                or not isinstance(commit, str)
                or len(commit) != 40
                or any(char not in "0123456789abcdef" for char in commit)
                or not isinstance(path, str)
                or not path
            ):
                raise PackageError("ClawHub returned an invalid GitHub source.")
            repo_owner, repo_name = repo.split("/")
            return _github(repo_owner, repo_name, ref=commit, path=path)
        archive = resolved.get("archive")
        version = archive.get("version") if isinstance(archive, dict) else None
        if resolved.get("installKind") != "archive":
            raise PackageError("ClawHub returned an unsupported installation source.")
    if not isinstance(version, str) or not version:
        raise PackageError("ClawHub did not return a downloadable version.")
    download = "https://clawhub.ai/api/v1/download?" + urlencode(
        {"slug": slug, "ownerHandle": owner, "version": version}
    )
    return SkillSource(
        unwrap_archive(read_archive(_download(download))),
        f"https://clawhub.ai/{owner}/skills/{slug}?{urlencode({'version': version})}",
        slug,
    )


def load_source(source: str, *, ref: str | None = None) -> SkillSource:
    """Support directories/archives, arbitrary download URLs and common source links."""
    if not source.lower().startswith(("http://", "https://")):
        if ref is not None:
            raise PackageError("A revision is supported only for GitHub or skills.sh sources.")
        local_path = Path(source).expanduser()
        if not local_path.is_absolute():
            raise PackageError("Local Skill sources must be absolute paths on the vBot server.")
        if is_redirect(local_path):
            raise PackageError("Skill source must not be a symlink or junction.")
        if local_path.is_dir():
            return SkillSource(read_directory(local_path), local_path.as_posix(), local_path.name)
        if not stat.S_ISREG(local_path.stat().st_mode):
            raise PackageError("Skill source must be a directory or an ordinary archive file.")
        with local_path.open("rb") as stream:
            data = stream.read(MAX_DOWNLOAD_BYTES + 1)
        return SkillSource(
            unwrap_archive(read_archive(data)), local_path.as_posix(), local_path.name.split(".")[0]
        )

    _url(source)
    url = urlsplit(source)
    parts = [unquote(part) for part in url.path.strip("/").split("/") if part]
    host = (url.hostname or "").casefold()
    if (
        host in {"github.com", "www.github.com"}
        and len(parts) >= 2
        and (len(parts) == 2 or parts[2] in {"tree", "blob"})
    ):
        owner, repo = parts[:2]
        repo = repo.removesuffix(".git")
        path = None
        revision = ref or "HEAD"
        if len(parts) > 2:
            if len(parts) < 4:
                raise PackageError("GitHub tree/blob URL is missing its revision.")
            if ref is not None:
                tail = "/".join(parts[3:])
                if tail != ref and not tail.startswith(ref + "/"):
                    raise PackageError(
                        "--ref must match the revision in this GitHub URL; "
                        "use the repository URL to select a different revision."
                    )
                path = tail[len(ref) :].lstrip("/") or None
            else:
                revision, *subpath = parts[3:]
                path = "/".join(subpath) or None
            if parts[2] == "blob":
                if path is None or path.rsplit("/", 1)[-1] != "SKILL.md":
                    raise PackageError("Use a GitHub Skill directory or SKILL.md link.")
                path = path.removesuffix("SKILL.md").rstrip("/") or "."
        return _github(owner, repo, ref=revision, path=path)
    if host in {"skills.sh", "www.skills.sh"} and len(parts) == 3:
        return _github(parts[0], parts[1], ref=ref or "HEAD", path=None, name=parts[2])
    if ref is not None:
        raise PackageError("A revision is supported only for GitHub or skills.sh sources.")
    if host in {"clawhub.ai", "www.clawhub.ai"} and (
        len(parts) == 2 or (len(parts) == 3 and parts[1] == "skills")
    ):
        return _clawhub(
            parts[0],
            parts[-1],
            parse_qs(url.query, keep_blank_values=True).get("version", [None])[0],
        )
    # Signed query parameters may be needed for the download, but never persist them.
    origin = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    return SkillSource(
        unwrap_archive(read_archive(_download(source))),
        origin,
        parts[-1].split(".")[0] if parts else "skill",
    )
