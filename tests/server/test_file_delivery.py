"""Signed original-file delivery and public chat projection tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import ChatMessage
from core.chat.output_files import AssistantFileReference
from core.sessions import SessionAddress
from server.app import create_app
from server.file_delivery import FILE_URL_PREFIX, FileDelivery
from server.rpc.payloads import _visible_message
from tests.server.test_rpc import StubAdapter, StubRuntime

_FILE_URL_PATTERN = re.compile(r"\(/api/files/([^\s)]+)\)")


def test_file_endpoint_serves_current_original_and_rejects_tampering(tmp_path: Path) -> None:
    image = tmp_path / "live image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    runtime = StubRuntime(tmp_path / "data", StubAdapter())
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        projected = cast(Any, client.app).state.file_delivery.project_message(
            _assistant_payload(image)
        )
        url = _only_file_url(cast(str, projected["content"]))
        first = client.get(url)

        image.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
        second = client.get(url)
        tampered = client.get(url[:-1] + ("A" if url[-1] != "A" else "B"))
        image.unlink()
        missing = client.get(url)

    assert first.status_code == 200
    assert first.content == b"\x89PNG\r\n\x1a\nfirst"
    assert first.headers["content-type"].startswith("image/png")
    assert first.headers["content-disposition"].startswith("inline;")
    assert "live%20image.png" in first.headers["content-disposition"]
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["cache-control"] == "no-cache"
    assert second.content == b"\x89PNG\r\n\x1a\nsecond"
    assert tampered.status_code == 404
    assert missing.status_code == 404


def test_non_image_file_is_projected_as_attachment_download(tmp_path: Path) -> None:
    report = tmp_path / "report [final].txt"
    report.write_text("current report", encoding="utf-8")
    runtime = StubRuntime(tmp_path / "data", StubAdapter())
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        projected = cast(Any, client.app).state.file_delivery.project_message(
            _assistant_payload(report)
        )
        content = cast(str, projected["content"])
        response = client.get(_only_file_url(content))

    assert content.startswith(r"[report \[final\].txt]")
    assert response.status_code == 200
    assert response.text == "current report"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_public_projection_is_fail_closed_and_regenerates_urls(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    message = ChatMessage.assistant(
        model="provider/model",
        content=f"Result:\n{image}",
        output_files=[AssistantFileReference(line_index=1, path=str(image.resolve()))],
    )

    without_delivery = _visible_message(message)
    first = _visible_message(message, file_delivery=FileDelivery(secret=b"first-secret"))
    second = _visible_message(message, file_delivery=FileDelivery(secret=b"second-secret"))

    assert without_delivery["content"] == message.content
    assert "output_files" not in without_delivery
    assert "output_files" not in first
    assert str(image) not in cast(str, first["content"])
    assert cast(str, first["content"]).startswith("Result:\n![image.png]")
    assert first["content"] != second["content"]


def test_rpc_final_message_and_history_share_signed_original_file_projection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "chart.png"
    report = workspace / "report.txt"
    image.write_bytes(b"\x89PNG\r\n\x1a\nchart-one")
    report.write_text("report-one", encoding="utf-8")
    content = f"Files: **file:{image}** and _file:{report}_"
    runtime = StubRuntime(
        tmp_path / "data",
        StubAdapter([{"content": content, "tool_calls": None}]),
    )
    runtime.agents.update(
        "coder",
        model="openai/gpt-5.2::api-key",
        workspace=str(workspace),
    )
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        created = client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )
        sent = client.post(
            "/api/rpc",
            json={
                "method": "chat.send",
                "params": {
                    "agent_id": "coder",
                    "session_id": "session-one",
                    "content": "Show files",
                },
            },
        )
        history = client.post(
            "/api/rpc",
            json={
                "method": "chat.history",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )

        sent_content = sent.json()["result"]["message"]["content"]
        history_content = next(
            message["content"]
            for message in reversed(history.json()["result"]["messages"])
            if message["role"] == "assistant"
        )
        urls = _file_urls(sent_content)
        image_response = client.get(urls[0])
        report_response = client.get(urls[1])

    assert created.json()["ok"] is True
    assert sent.json()["ok"] is True
    assert "output_files" not in sent.text
    assert str(image) not in sent_content
    assert sent_content == history_content
    assert sent_content.startswith("Files: ![chart.png]")
    assert " and [report.txt]" in sent_content
    assert image_response.content == b"\x89PNG\r\n\x1a\nchart-one"
    assert report_response.text == "report-one"
    canonical = runtime.chat_sessions.get(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    ).load()[-2]
    assert canonical.content == content
    assert canonical.output_files is not None
    assert [reference.path for reference in canonical.output_files] == [
        str(image.resolve()),
        str(report.resolve()),
    ]


def _assistant_payload(path: Path) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": str(path),
        "output_files": [{"line_index": 0, "path": str(path.resolve())}],
    }


def _only_file_url(content: str) -> str:
    urls = _file_urls(content)
    assert len(urls) == 1
    return urls[0]


def _file_urls(content: str) -> list[str]:
    return [f"{FILE_URL_PREFIX}{match.group(1)}" for match in _FILE_URL_PATTERN.finditer(content)]


def test_tool_images_deliver_originals_and_remain_addressable_when_missing(tmp_path: Path) -> None:
    from server.rpc.payloads import remove_opaque_provider_metadata

    image = tmp_path / "original image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    display = {"image_files": [{"path": str(image), "filename": image.name}]}
    message = ChatMessage.tool(
        tool_call_id="call", name="analyze_image", content="{}", tool_display=display
    )
    runtime = StubRuntime(tmp_path / "data", StubAdapter())
    app = create_app(runtime=cast(Any, runtime))
    with TestClient(app) as client:
        delivery = cast(Any, client.app).state.file_delivery
        public = _visible_message(message, file_delivery=delivery)
        preview = public["tool_display"]["images"][0]
        assert preview["filename"] == image.name
        assert "path" not in preview
        assert "image_files" not in public["tool_display"]
        url = preview["url"]
        assert client.get(url).content == image.read_bytes()
        image.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
        assert client.get(url).content == image.read_bytes()
        image.unlink()
        assert client.get(url).status_code == 404
        reloaded = _visible_message(message, file_delivery=delivery)
        assert reloaded["tool_display"]["images"][0]["url"] == url
        event = remove_opaque_provider_metadata(
            {"payload": {"display": display}}, file_delivery=delivery
        )
        assert event["payload"]["display"] == public["tool_display"]
    restarted = _visible_message(message, file_delivery=FileDelivery(secret=b"restart"))
    assert restarted["tool_display"]["images"][0]["url"] != url
    assert "image_files" not in _visible_message(message)["tool_display"]


def test_tool_image_projection_rejects_non_absolute_paths(tmp_path: Path) -> None:
    delivery = FileDelivery()
    projected = delivery.project_message(
        {
            "image_files": [
                {"path": "relative.png"},
                {"path": "https://example.com/image.png"},
                {"path": str(tmp_path / "bad\0name")},
                None,
            ]
        }
    )
    assert projected == {"images": []}
