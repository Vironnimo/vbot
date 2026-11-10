"""Conservative, offline recognition of complete API keys in human input.

The same rule data drives browser storage protection. These are format checks,
not credential verification; unfamiliar formats and ordinary assignments remain
literal text. Prefix detection is only for keeping unfinished drafts off disk.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DetectedApiKey:
    start: int
    end: int
    value: str
    name: str
    provider: str


_SPEC = json.loads(Path(__file__).with_name("api_key_rules.json").read_text(encoding="utf-8"))
_TOKEN = _SPEC["token_characters"]
_RULES = tuple(
    (
        re.compile(rf"(?<![{_TOKEN}])(?:{rule['pattern']})(?![{_TOKEN}])"),
        rule["name"],
        rule["provider"],
    )
    for rule in _SPEC["rules"]
)
_PREFIXES = re.compile(
    rf"(?<![{_TOKEN}])(?:{'|'.join(re.escape(prefix) for prefix in _SPEC['draft_prefixes'])})"
)


def detect_api_keys(text: str) -> list[DetectedApiKey]:
    """Return complete recognized values in text order, with exact source spans."""
    found = []
    for pattern, name, provider in _RULES:
        for match in pattern.finditer(text):
            body = match.group(1)
            # Obvious repeated placeholder bodies are not credentials to capture.
            if _entropy(body) < _SPEC["minimum_entropy"]:
                continue
            found.append(DetectedApiKey(match.start(), match.end(), match.group(), name, provider))
    return sorted(found, key=lambda item: item.start)


def contains_potential_api_key(text: str) -> bool:
    """Whether a draft has a key prefix, even before its value is complete.

    Never use this more cautious predicate to change submitted message content.
    """
    return _PREFIXES.search(text) is not None


def _entropy(value: str) -> float:
    size = len(value)
    return -sum((count / size) * math.log2(count / size) for count in Counter(value).values())
