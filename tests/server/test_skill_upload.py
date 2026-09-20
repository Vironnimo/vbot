"""Browser archive ingress exercises the same installer as link/RPC imports."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from core.runtime import Runtime
from core.utils.config import Config
from server.app import create_app


def package(body=b"Read this guide.", *, unsafe=False):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "demo/SKILL.md", b"---\nname: demo\ndescription: A demo skill\n---\n" + body
        )
        archive.writestr("demo/assets/sample.bin", b"\x00\xff")
        if unsafe:
            archive.writestr("../escaped.txt", b"escape")
    return stream.getvalue()


@pytest.fixture
def client(tmp_path):
    app = create_app(runtime=Runtime(Config(data_dir=tmp_path / "data")))
    with TestClient(app) as client:
        app.state.runtime.agents.create("reader", "Reader")
        yield client


@pytest.mark.parametrize("scope", ["global", "agent:reader"])
def test_preview_install_read_and_replace(client, tmp_path, scope):
    def upload(data, **params):
        return client.post(
            "/api/skills/install",
            params={"scope": scope, **params},
            files={"file": ("example.skill", data, "application/octet-stream")},
        )

    preview = upload(package(), dry_run="true")
    assert preview.status_code == 200
    result = preview.json()
    assert result["operation"] == "preview"
    root = tmp_path / "data" / ("skills" if scope == "global" else "agents/reader/skills")
    assert not (root / "demo").exists()
    response = upload(package(), expected_sha256=result["sha256"])
    assert response.status_code == 200
    assert response.json()["operation"] == "installed"
    assert response.json()["scope"] == scope
    assert (root / "demo/assets/sample.bin").read_bytes() == b"\x00\xff"
    inventory = client.post("/api/rpc", json={"method": "skill.inventory"}).json()["result"]
    assert any(
        item["name"] == "demo" and item["editable_scope"] == scope for item in inventory["skills"]
    )
    assert upload(package()).json()["operation"] == "unchanged"
    assert (
        upload(package(b"Changed"), replace="true", expected_sha256=result["sha256"]).status_code
        == 400
    )
    assert upload(package(b"Changed")).status_code == 400
    assert upload(package(b"Changed"), replace="true").json()["operation"] == "replaced"


@pytest.mark.parametrize(
    "params,data,status",
    [
        ({"scope": "agent:missing"}, package(), 400),
        ({"scope": "project:example"}, package(), 400),
        ({"scope": "global", "replace": "yes"}, package(), 422),
        ({"scope": "global", "source": "C:/anything"}, package(), 422),
        ({"scope": "global"}, package(unsafe=True), 400),
        ({"scope": "global"}, b"not an archive", 400),
    ],
    ids=[
        "missing-agent",
        "project-scope",
        "invalid-boolean",
        "unknown-field",
        "traversal",
        "invalid-archive",
    ],
)
def test_rejected_uploads_leave_no_skill(client, tmp_path, params, data, status):
    response = client.post(
        "/api/skills/install", params=params, files={"file": ("demo.skill", data)}
    )
    assert response.status_code == status
    assert not (tmp_path / "data/skills/demo").exists()


def test_upload_limit_and_origin_guard_apply_before_installation(client, monkeypatch):
    import server.app as app_module

    monkeypatch.setattr(app_module, "SKILL_ARCHIVE_MAX_BYTES", 32)
    response = client.post(
        "/api/skills/install?scope=global", files={"file": ("demo.skill", package())}
    )
    assert response.status_code == 413
    response = client.post(
        "/api/skills/install?scope=global",
        files={"file": ("demo.skill", package())},
        headers={"Origin": "https://foreign.example"},
    )
    assert response.status_code == 403
