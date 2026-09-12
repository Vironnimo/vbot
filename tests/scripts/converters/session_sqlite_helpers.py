"""session sqlite helpers coverage."""

from __future__ import annotations

import json
from pathlib import Path

from core.chat import ChatMessage


def _write_transcript(path: Path, content: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ChatMessage.user(content).to_dict()) + "\n", encoding="utf-8")
