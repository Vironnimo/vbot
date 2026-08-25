"""Tests for skill CLI parsing and RPC commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import skill_management
from cli.server_management import ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=resolve_daily_log_path(data_dir),
    )


def test_parse_args_supports_skill_catalog_command() -> None:
    args = cli_main.parse_args(
        ["skill", "list", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "skill"
    assert args.command == "list"
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_list_skills_posts_catalog_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [{"name": "summarize", "description": "Summarize long text"}],
                    "invalid_skills": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.list_skills(instance)

    assert result.ok is True
    assert result.instance is instance
    assert "- summarize  Summarize long text" in result.message
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "skill.list", "params": {}},
            "timeout": 10.0,
        }
    ]


def test_list_skills_formats_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "skill.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {"name": "draft-email", "description": "Draft concise replies"},
                        {"name": "release-notes", "description": "Write release notes"},
                    ],
                    "invalid_skills": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.list_skills(instance)

    assert result.ok is True
    assert result.instance == instance
    assert result.message.splitlines()[1:] == [
        "- draft-email  Draft concise replies",
        "- release-notes  Write release notes",
    ]


def test_list_skills_returns_empty_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"skills": [], "invalid_skills": []}},
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.list_skills(instance)

    assert result.ok is True
    assert result.instance is instance
    assert result.message.strip()


def test_list_skills_formats_requirement_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {
                            "name": "native-build",
                            "description": "Build native projects",
                            "state": "unavailable",
                            "requirements": {
                                "missing": ["missing binary 'gcc'"],
                                "optional_missing": ["missing binary 'jq'"],
                            },
                        }
                    ],
                    "invalid_skills": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.list_skills(instance)

    assert result.ok is True
    assert result.message.splitlines()[1:] == [
        "- native-build  Build native projects "
        "(unavailable: missing binary 'gcc'; optional missing: missing binary 'jq')",
    ]


def test_list_skills_includes_invalid_section_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {"name": "summarize", "description": "Summarize long text"},
                    ],
                    "invalid_skills": [
                        {
                            "name": "broken-skill",
                            "path": "C:/skills/broken-skill/SKILL.md",
                            "warnings": ["missing description"],
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.list_skills(instance)

    assert result.ok is True
    assert result.instance == instance
    assert "skills:" in result.message
    assert "invalid skills:" in result.message
    assert "- broken-skill (C:/skills/broken-skill/SKILL.md): missing description" in result.message


def test_list_skills_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "ok": False,
                "error": {"code": "rpc_error", "message": "server exploded"},
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.list_skills(instance)

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("rpc_error:")


def test_skill_editable_scope_crud_and_supporting_file_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        result: dict[str, Any]
        if json["method"] == "skill.read":
            result = {
                "skills": [
                    {
                        "name": "librarian",
                        "description": "Maintain the catalog",
                        "content": "---\nname: librarian\n---\n",
                    }
                ]
            }
        else:
            operation = {
                "skill.create": "created",
                "skill.update": "updated",
                "skill.delete": "deleted",
                "skill.write_file": "wrote_file",
                "skill.remove_file": "removed_file",
            }[json["method"]]
            result = {"name": "librarian", "operation": operation, "warnings": []}
        return httpx.Response(200, json={"ok": True, "result": result})

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    read = skill_management.skill_read(instance, "agent:assistant")
    created = skill_management.skill_create(
        instance, "agent:assistant", "librarian", "# Librarian", "cli"
    )
    updated = skill_management.skill_update(
        instance, "agent:assistant", "librarian", "# Updated", None
    )
    wrote = skill_management.skill_write_file(
        instance, "agent:assistant", "librarian", "references/schema.md", "# Schema"
    )
    removed_file = skill_management.skill_remove_file(
        instance, "agent:assistant", "librarian", "references/schema.md", True
    )
    deleted = skill_management.skill_delete(instance, "agent:assistant", "librarian", True)

    assert "agent:assistant" in read.message
    assert "created" in created.message and "librarian" in created.message
    assert "updated" in updated.message and "librarian" in updated.message
    assert "wrote_file" in wrote.message and "librarian" in wrote.message
    assert "removed_file" in removed_file.message and "librarian" in removed_file.message
    assert "deleted" in deleted.message and "librarian" in deleted.message
    assert [call["method"] for call in calls] == [
        "skill.read",
        "skill.create",
        "skill.update",
        "skill.write_file",
        "skill.remove_file",
        "skill.delete",
    ]


def test_skill_destructive_commands_require_confirmation(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    assert skill_management.skill_delete(instance, "global", "librarian", False).ok is False
    assert (
        skill_management.skill_remove_file(
            instance, "global", "librarian", "references/schema.md", False
        ).ok
        is False
    )


def test_parse_args_supports_private_skill_create() -> None:
    args = cli_main.parse_args(
        [
            "skill",
            "create",
            "librarian",
            "--scope",
            "agent:assistant",
            "--content",
            "# Librarian",
        ]
    )

    assert args.command == "create"
    assert args.scope == "agent:assistant"
    assert args.content == "# Librarian"


def test_skill_inventory_posts_inventory_rpc_and_formats_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "skill.inventory", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {
                            "name": "librarian",
                            "description": "Maintain the catalog",
                            "origin": "agent",
                            "owner_id": "assistant",
                            "status": "available",
                            "shared_with": ["researcher"],
                            "missing": [],
                            "optional_missing": [],
                            "warnings": [],
                        },
                        {
                            "name": "native-build",
                            "description": "Build native projects",
                            "origin": "bundled",
                            "owner_id": None,
                            "status": "disabled",
                            "shared_with": [],
                            "missing": ["missing binary 'gcc'"],
                            "optional_missing": ["missing binary 'jq'"],
                            "warnings": ["duplicate name"],
                        },
                    ],
                    "stale_shared": [{"agent_id": "ghost", "name": "gone"}],
                    "policy_diagnostics": ["policy file warning"],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_inventory(instance)

    assert result.ok is True
    message = result.message
    assert "- librarian  Maintain the catalog  [agent]" in message
    assert "status: available; owner: assistant; shared_with: researcher" in message
    assert "- native-build  Build native projects  [bundled]" in message
    assert (
        "status: disabled; owner: -; shared_with: -; "
        "optional missing: missing binary 'jq'; warnings: duplicate name" in message
    )
    assert "stale shared entries (owner or package no longer exists):" in message
    assert "- ghost: gone" in message
    assert "policy diagnostics:" in message
    assert "- policy file warning" in message


def test_skill_inventory_reports_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"skills": [], "stale_shared": [], "policy_diagnostics": []},
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_inventory(instance)

    assert result.ok is True
    assert result.message == "no skills found in any source"


def test_skill_disable_and_enable_post_policy_toggle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"name": "librarian"}})

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    disabled = skill_management.skill_set_disabled(instance, "librarian", True)
    enabled = skill_management.skill_set_disabled(instance, "librarian", False)

    assert disabled.ok is True
    assert disabled.message == "disabled skill librarian"
    assert enabled.message == "enabled skill librarian"
    assert [call["params"] for call in calls] == [
        {"name": "librarian", "disabled": True},
        {"name": "librarian", "disabled": False},
    ]


def test_skill_share_and_unshare_post_share_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        if json["params"].get("shared"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "agent_id": "assistant",
                        "name": "librarian",
                        "receivers": ["researcher", "coder"],
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    shared = skill_management.skill_share(instance, "assistant", "librarian", ["researcher"])
    unshared = skill_management.skill_unshare(instance, "assistant", "librarian")

    assert shared.ok is True
    assert shared.message == "shared skill librarian from assistant to: researcher, coder"
    assert unshared.message == "unshared skill librarian from assistant"
    assert [call["params"] for call in calls] == [
        {"agent_id": "assistant", "name": "librarian", "shared": True, "receivers": ["researcher"]},
        {"agent_id": "assistant", "name": "librarian", "shared": False},
    ]


def test_parse_args_supports_manager_commands() -> None:
    inventory = cli_main.parse_args(["skill", "inventory"])
    disable = cli_main.parse_args(["skill", "disable", "librarian"])
    enable = cli_main.parse_args(["skill", "enable", "librarian"])
    share = cli_main.parse_args(
        ["skill", "share", "assistant", "librarian", "--to", "researcher", "--to", "coder"]
    )
    unshare = cli_main.parse_args(["skill", "unshare", "assistant", "librarian"])

    assert (inventory.area, inventory.command) == ("skill", "inventory")
    assert (disable.command, disable.name) == ("disable", "librarian")
    assert (enable.command, enable.name) == ("enable", "librarian")
    assert (share.agent, share.name, share.receivers) == (
        "assistant",
        "librarian",
        ["researcher", "coder"],
    )
    assert (unshare.agent, unshare.name) == ("assistant", "librarian")


def test_parse_args_share_requires_receiver() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args(["skill", "share", "assistant", "librarian"])

    assert exc_info.value.code == 2


def test_skill_disable_unknown_name_attaches_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        if json["method"] == "skill.set_disabled":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error": {"code": "invalid_request", "message": "unknown skill: 'librrarian'"},
                },
            )
        assert json["method"] == "skill.inventory"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {"name": "librarian", "origin": "bundled"},
                        {"name": "summarize", "origin": "global"},
                    ],
                    "stale_shared": [],
                    "policy_diagnostics": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_set_disabled(instance, "librrarian", True)

    assert result.ok is False
    assert "unknown skill: 'librrarian'" in result.message
    assert "did you mean: librarian" in result.message
    assert "known skills: librarian, summarize" in result.message


def test_skill_share_unknown_owner_lists_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        if json["method"] == "skill.share":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error": {
                        "code": "invalid_request",
                        "message": "unknown agent: 'assistnt' (sharing is identity-agent-only)",
                    },
                },
            )
        assert json["method"] == "agent.list"
        return httpx.Response(
            200,
            json={"ok": True, "result": {"agents": [{"id": "assistant"}, {"id": "coder"}]}},
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_share(instance, "assistnt", "librarian", ["coder"])

    assert result.ok is False
    assert "did you mean: assistant" in result.message
    assert "available agents: assistant, coder" in result.message


def test_skill_share_wrong_private_skill_lists_owned_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        if json["method"] == "skill.share":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error": {
                        "code": "invalid_request",
                        "message": "agent 'assistant' owns no private skill named 'ghost'",
                    },
                },
            )
        assert json["method"] == "skill.inventory"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {"name": "notes", "owner_id": "assistant"},
                        {"name": "other", "owner_id": "coder"},
                    ],
                    "stale_shared": [],
                    "policy_diagnostics": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_share(instance, "assistant", "ghost", ["coder"])

    assert result.ok is False
    assert "owns no private skill named 'ghost'" in result.message
    assert "assistant's private skills: notes" in result.message
