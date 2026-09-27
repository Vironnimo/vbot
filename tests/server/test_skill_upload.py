"""Browser archive ingress exercises the same installer as link/RPC imports."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

import server.app as app_module
from core.runtime import Runtime
from core.skills.authoring import SkillAuthoringService
from core.utils.config import Config
from server.app import create_app
from tests.server.app_test_support import ServerStubRuntime


def package(body: bytes = b"Read this guide.", *, unsafe: bool = False) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "demo/SKILL.md", b"---\nname: demo\ndescription: A demo skill\n---\n" + body
        )
        archive.writestr("demo/assets/sample.bin", b"\x00\xff")
        if unsafe:
            archive.writestr("../escaped.txt", b"escape")
    return stream.getvalue()


def test_skill_archive_upload_previews_installs_and_replaces_in_each_scope(
    tmp_path: Path,
) -> None:
    # Real Runtime (about a second): the installed package must reach the live inventory.
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))

    with TestClient(app) as client:
        app.state.runtime.agents.create("reader", "Reader")
        for scope, root in [
            ("global", tmp_path / "data" / "skills"),
            ("agent:reader", tmp_path / "data" / "agents" / "reader" / "skills"),
        ]:

            def upload(data: bytes, scope: str = scope, **params: str) -> dict[str, object]:
                response = client.post(
                    "/api/skills/install",
                    params={"scope": scope, **params},
                    files={"file": ("example.skill", data, "application/octet-stream")},
                )
                return {"status": response.status_code, **response.json()}

            preview = upload(package(), dry_run="true")
            previewed_nothing = not (root / "demo").exists()
            installed = upload(package(), expected_sha256=str(preview["sha256"]))
            inventory = client.post("/api/rpc", json={"method": "skill.inventory"}).json()
            unchanged = upload(package())
            stale_replace = upload(
                package(b"Changed"), replace="true", expected_sha256=str(preview["sha256"])
            )
            without_replace = upload(package(b"Changed"))
            replaced = upload(package(b"Changed"), replace="true")

            assert (preview["status"], preview["operation"]) == (200, "preview")
            assert previewed_nothing
            assert (installed["operation"], installed["scope"]) == ("installed", scope)
            assert (root / "demo" / "assets" / "sample.bin").read_bytes() == b"\x00\xff"
            assert any(
                item["name"] == "demo" and item["editable_scope"] == scope
                for item in inventory["result"]["skills"]
            )
            assert unchanged["operation"] == "unchanged"
            assert stale_replace["status"] == without_replace["status"] == 400
            assert replaced["operation"] == "replaced"


def test_rejected_skill_uploads_leave_no_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / "skills"
    runtime = ServerStubRuntime(
        tmp_path / "data",
        agents=SimpleNamespace(exists=lambda agent_id: agent_id == "reader"),
        global_skills_dir=skills_dir,
        agent_skills_dir=lambda agent_id: tmp_path / "agents" / agent_id / "skills",
        skill_authoring=SkillAuthoringService(protected_roots=[]),
    )
    rejections = [
        ({"scope": "agent:missing"}, package(), 400),
        ({"scope": "project:example"}, package(), 400),
        ({"scope": "global", "replace": "yes"}, package(), 422),
        ({"scope": "global", "source": "C:/anything"}, package(), 422),
        ({"scope": "global"}, package(unsafe=True), 400),
        ({"scope": "global"}, b"not an archive", 400),
    ]

    with TestClient(create_app(runtime=runtime)) as client:
        statuses = [
            client.post(
                "/api/skills/install", params=params, files={"file": ("demo.skill", data)}
            ).status_code
            for params, data, _status in rejections
        ]
        foreign_origin = client.post(
            "/api/skills/install?scope=global",
            files={"file": ("demo.skill", package())},
            headers={"Origin": "https://foreign.example"},
        )
        monkeypatch.setattr(app_module, "SKILL_ARCHIVE_MAX_BYTES", 32)
        oversized = client.post(
            "/api/skills/install?scope=global", files={"file": ("demo.skill", package())}
        )

    assert statuses == [status for _params, _data, status in rejections]
    assert foreign_origin.status_code == 403
    assert oversized.status_code == 413
    assert not skills_dir.exists()
    assert not (tmp_path / "agents").exists()
