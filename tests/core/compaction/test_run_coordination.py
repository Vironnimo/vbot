"""Compaction run coordination boundary: Chat is reached only through its public host contract."""

import ast
from pathlib import Path

from core.compaction.run_coordination import CompactionRunCoordinator


def test_coordinator_imports_no_private_chat_contracts() -> None:
    source_path = Path(CompactionRunCoordinator.__module__.replace(".", "/") + ".py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    private_chat_imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("core.chat")
        for alias in node.names
        if alias.name.startswith("_")
    ]

    assert private_chat_imports == []
