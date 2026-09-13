"""File Tool probes with actual files, production reads, writes, and stale guards."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.tools import FileReadState, ToolContext, ToolRegistry, register_read_tool, tool_failure
from core.tools.write import register_write_tool


def file_tolerance_cases() -> list[dict[str, Any]]:
    return [
        {"id": "plain", "path": "folder/note.txt", "content": "false"},
        {"id": "empty", "path": "folder/note.txt", "content": ""},
        {
            "id": "quoted",
            "path": ' "folder/note.txt" ' if os.name == "nt" else "folder/note.txt",
            "content": "new",
        },
        {
            "id": "backslash",
            "path": "folder\\note.txt" if os.name == "nt" else "folder/note.txt",
            "content": "new",
        },
        {
            "id": "read_existing",
            "path": '"folder/note.txt"' if os.name == "nt" else "folder/note.txt",
            "content": "new",
            "existing": True,
        },
        {
            "id": "unread_existing",
            "path": '"folder/note.txt"' if os.name == "nt" else "folder/note.txt",
            "content": "new",
            "existing": True,
            "success": False,
        },
    ]


async def file_case(adapter: Any, args: argparse.Namespace, case: dict[str, Any]) -> dict[str, Any]:
    class FixtureContext(ToolContext):
        def resolve_path(self, path: str | Path) -> Path:
            target = super().resolve_path(path)
            if not target.is_relative_to(self.workspace):
                raise ValueError("Only the disposable fixture directory is in scope")
            return target

    with TemporaryDirectory(prefix="vbot-file-tolerance-") as temporary:
        root = Path(temporary).resolve()
        target = root / "folder" / "note.txt"
        if case.get("existing"):
            target.parent.mkdir()
            target.write_text("original", encoding="utf-8")
        registry = ToolRegistry()
        state = FileReadState()
        register_write_tool(registry, file_state=state)
        register_read_tool(
            registry,
            attachment_store=None,
            speech_service=None,
            file_state=state,
            speech_max_size_bytes=10000,
        )
        context = FixtureContext(
            agent_id="probe",
            session_id="probe",
            run_id="probe",
            tool_call_id="fixture",
            tool_name="write",
            tool_call_index=0,
            workspace=root,
            vbot_root=root,
            data_root=root,
        )
        if case["id"] == "read_existing":
            read = await registry.dispatch(
                replace(context, tool_name="read"), {"path": "folder/note.txt"}
            )
            assert read["ok"]
        request = {"path": case["path"], "content": case["content"]}
        raw = await adapter.send(
            [
                {
                    "role": "system",
                    "content": "Make one diagnostic write call. Preserve the parsed JSON "
                    "values exactly. Keep extra quote characters and backslashes inside the "
                    "path value, but do not add quotes to content. "
                    "Only the disposable fixture directory is in scope.",
                },
                {"role": "user", "content": "Arguments: " + json.dumps(request)},
            ],
            tools=registry.provider_definitions(["write"]),
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 2500,
        )
        calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
        results = []
        for call in calls:
            try:
                results.append(
                    await registry.dispatch(
                        replace(context, tool_name=call["name"]), call["arguments"], ["write"]
                    )
                )
            except ValueError as error:
                results.append(tool_failure("invalid_arguments", str(error)))
        files = {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in root.rglob("*")
            if p.is_file()
        }
        checks = {
            "one_call": len(calls) == len(results) == 1,
            "file_effect": files
            == {"folder/note.txt": case["content"] if case.get("success", True) else "original"},
        }
        if case.get("success", True):
            checks["success"] = bool(results) and results[0]["ok"]
            read = await registry.dispatch(
                replace(context, tool_name="read"), {"path": case["path"]}
            )
            checks["equivalent_read"] = read["ok"]
        else:
            checks["guard"] = (
                bool(results) and (results[0].get("error") or {}).get("code") == "file_not_read"
            )
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": calls,
            "results": results,
            "files": files,
        }
