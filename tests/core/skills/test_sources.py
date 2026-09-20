"""URL import uses complete, bounded downloads and preserves exact source selection."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
import respx

from core.skills import SkillAuthoringError, SkillAuthoringService, _sources

SHA = "a" * 40


def bundle(*names: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in names:
            archive.writestr(
                f"repo-main/skills/{name}/SKILL.md",
                f"---\nname: {name}\ndescription: Do a task.\n---\nBody\n",
            )
            archive.writestr(f"repo-main/skills/{name}/assets/raw.bin", b"\x00\xff\x01")
    return stream.getvalue()


@respx.mock
def test_arbitrary_download_with_redirect_preserves_binary_and_redacts_query(tmp_path):
    respx.get("https://downloads.example/get?token=secret").respond(
        302, headers={"Location": "https://cdn.example/package"}
    )
    respx.get("https://cdn.example/package").respond(content=bundle("demo"))
    result = SkillAuthoringService().install(
        tmp_path / "skills", "https://downloads.example/get?token=secret"
    )
    assert result.name == "demo"
    assert (tmp_path / "skills/demo/assets/raw.bin").read_bytes() == b"\x00\xff\x01"
    receipt = json.loads((tmp_path / "skills/demo/.vbot-install.json").read_bytes())
    assert receipt["source"] == "https://downloads.example/get"
    assert "secret" not in repr(result)


@pytest.mark.parametrize(
    "url,ref,commit_path",
    [
        ("https://github.com/owner/repo/tree/main/skills/demo", None, "main"),
        ("https://github.com/owner/repo/blob/main/skills/demo/SKILL.md", None, "main"),
        (
            "https://github.com/owner/repo/tree/feature/docs/skills/demo",
            "feature/docs",
            "feature%2Fdocs",
        ),
        ("https://skills.sh/owner/repo/demo", None, "HEAD"),
    ],
)
@respx.mock
def test_source_links_select_only_requested_package_at_resolved_commit(
    tmp_path, url, ref, commit_path
):
    respx.get(f"https://api.github.com/repos/owner/repo/commits/{commit_path}").respond(
        json={"sha": SHA}
    )
    respx.get(f"https://codeload.github.com/owner/repo/zip/{SHA}").respond(
        content=bundle("demo", "other")
    )
    result = SkillAuthoringService().install(tmp_path / "skills", url, ref=ref)
    assert result.name == "demo"
    assert result.source == f"https://github.com/owner/repo/tree/{SHA}"
    assert result.package_path == "skills/demo"
    assert list((tmp_path / "skills").iterdir()) == [tmp_path / "skills/demo"]


@respx.mock
def test_github_root_lists_and_selects_multiple_skills(tmp_path):
    respx.get("https://api.github.com/repos/owner/repo/commits/HEAD").respond(json={"sha": SHA})
    respx.get(f"https://codeload.github.com/owner/repo/zip/{SHA}").respond(
        content=bundle("demo", "other")
    )
    service = SkillAuthoringService()
    result = service.install(tmp_path / "skills", "https://github.com/owner/repo", dry_run=True)
    assert {item["path"] for item in result.candidates} == {"skills/demo", "skills/other"}
    assert not (tmp_path / "skills").exists()
    result = service.install(
        tmp_path / "skills", "https://github.com/owner/repo", path="skills/other"
    )
    assert result.name == "other"


@pytest.mark.parametrize(
    "url",
    [
        "https://clawhub.ai/author/demo",
        "https://clawhub.ai/author/skills/demo",
        "https://clawhub.ai/author/skills/demo?version=1.2.3",
    ],
)
@respx.mock
def test_catalog_publisher_is_verified_and_version_pinned(tmp_path, url):
    respx.get("https://clawhub.ai/api/v1/skills/demo", params={"ownerHandle": "author"}).respond(
        json={"owner": {"handle": "author"}, "latestVersion": {"version": "1.2.3"}}
    )
    if "?version=" not in url:
        respx.get(
            "https://clawhub.ai/api/v1/skills/demo/install", params={"ownerHandle": "author"}
        ).respond(
            json={
                "ok": True,
                "slug": "demo",
                "installKind": "archive",
                "archive": {"version": "1.2.3"},
            }
        )
    download = respx.get(
        "https://clawhub.ai/api/v1/download",
        params={"slug": "demo", "ownerHandle": "author", "version": "1.2.3"},
    ).respond(content=bundle("demo"))
    result = SkillAuthoringService().install(tmp_path / "skills", url)
    assert result.source == "https://clawhub.ai/author/skills/demo?version=1.2.3"
    assert download.call_count == 1


@respx.mock
def test_clawhub_github_handoff_uses_exact_public_commit_and_directory(tmp_path):
    respx.get("https://clawhub.ai/api/v1/skills/demo?ownerHandle=author").respond(
        json={"owner": {"handle": "author"}, "latestVersion": None}
    )
    respx.get("https://clawhub.ai/api/v1/skills/demo/install?ownerHandle=author").respond(
        json={
            "ok": True,
            "slug": "demo",
            "installKind": "github",
            "github": {"repo": "owner/repo", "commit": SHA, "path": "skills/demo"},
        }
    )
    respx.get(f"https://api.github.com/repos/owner/repo/commits/{SHA}").respond(json={"sha": SHA})
    respx.get(f"https://codeload.github.com/owner/repo/zip/{SHA}").respond(content=bundle("demo"))
    result = SkillAuthoringService().install(
        tmp_path / "skills", "https://clawhub.ai/author/skills/demo"
    )
    assert result.name == "demo"
    assert result.source == f"https://github.com/owner/repo/tree/{SHA}"
    assert result.package_path == "skills/demo"


@respx.mock
def test_blocked_catalog_resolver_is_not_bypassed(tmp_path):
    respx.get("https://clawhub.ai/api/v1/skills/demo?ownerHandle=author").respond(
        json={"owner": {"handle": "author"}, "latestVersion": {"version": "1.2.3"}}
    )
    respx.get("https://clawhub.ai/api/v1/skills/demo/install?ownerHandle=author").respond(403)
    with pytest.raises(SkillAuthoringError, match="HTTP 403"):
        SkillAuthoringService().install(tmp_path / "skills", "https://clawhub.ai/author/demo")
    assert not (tmp_path / "skills").exists()


@respx.mock
def test_wrong_publisher_is_not_replaced_by_same_slug(tmp_path):
    respx.get("https://clawhub.ai/api/v1/skills/demo?ownerHandle=author").respond(
        json={"owner": {"handle": "someone-else"}}
    )
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(tmp_path / "skills", "https://clawhub.ai/author/demo")
    assert not (tmp_path / "skills").exists()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, text="<html>A catalog page</html>"),
        httpx.Response(302, headers={"Location": "http://downloads.example/package"}),
        httpx.Response(302, headers={"Location": "file:///private/secret"}),
        httpx.Response(200, headers={"Content-Length": "999999999"}),
    ],
)
@respx.mock
def test_failed_or_unsafe_download_does_not_publish(tmp_path, response):
    respx.get("https://downloads.example/package").mock(return_value=response)
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(tmp_path / "skills", "https://downloads.example/package")
    assert not (tmp_path / "skills").exists()


@respx.mock
def test_actual_download_bytes_are_bounded_without_content_length(tmp_path, monkeypatch):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 100

    respx.get("https://downloads.example/package").respond(stream=Stream())
    with pytest.raises(_sources.PackageError):
        _sources._download("https://downloads.example/package", limit=10)


@respx.mock
def test_slow_small_chunks_cannot_evade_the_download_deadline(monkeypatch):
    elapsed = 0

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal elapsed
            for _ in range(10):
                elapsed += 30
                yield b"x"

    monkeypatch.setattr(_sources.time, "monotonic", lambda: elapsed)
    respx.get("https://downloads.example/slow").respond(stream=Stream())
    with pytest.raises(_sources.PackageError, match="time limit"):
        _sources._download("https://downloads.example/slow")


@pytest.mark.parametrize("url", ["https://user:secret@example.com/a.skill", "https:///no-host"])
def test_credential_urls_fail_without_leaking_the_value(tmp_path, url):
    with pytest.raises(SkillAuthoringError) as failure:
        SkillAuthoringService().install(tmp_path / "skills", url)
    assert "secret" not in str(failure.value)


@respx.mock
def test_source_path_constraint_is_not_discarded(tmp_path):
    respx.get("https://api.github.com/repos/owner/repo/commits/main").respond(json={"sha": SHA})
    respx.get(f"https://codeload.github.com/owner/repo/zip/{SHA}").respond(
        content=bundle("demo", "other")
    )
    with pytest.raises(SkillAuthoringError):
        SkillAuthoringService().install(
            tmp_path / "skills",
            "https://github.com/owner/repo/tree/main/skills/demo",
            path="skills/other",
        )
    assert not (tmp_path / "skills").exists()
