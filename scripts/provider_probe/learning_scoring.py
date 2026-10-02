"""Programmatic scoring of learning evaluations: observed Tool use and state change.

Scoring sees only the Tool calls, their results and the Memory and Skill state
before and after an attempt; expectations live in the case fixture and never
reach the Model. An attempt passes when the Model finished with a final answer,
its effect matches one acceptable outcome, and no violation occurred.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

MEMORY_MUTATIONS = frozenset({"add", "replace", "remove"})
# Name segments that mark a ticket, incident or one-off fix rather than a task class.
_TICKET_SEGMENTS = frozenset({"ticket", "jira", "incident", "hotfix"})
# Sentences with these words state a condition, not an enduring claim.
_CONDITIONAL = re.compile(r"\b(if|when|whenever|unless|in case)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def ticket_like_skill_name(name: str, extra_tokens: Sequence[str] = ()) -> bool:
    """Return whether a new Skill's name looks like a ticket, incident or codename."""
    lowered = name.lower()
    if any(character.isdigit() for character in lowered):
        return True
    segments = set(re.split(r"[-_]+", lowered))
    return bool(segments & _TICKET_SEGMENTS) or any(
        token.lower() in lowered for token in extra_tokens
    )


class CallObserver:
    """Collect process violations while an attempt's Tool calls run.

    A Memory write needs a current ``list`` of its scope in this attempt; a new
    Skill needs a current catalog listing; a write to an existing own Skill file
    needs a ``skill`` read of that file. Writes that target a protected Skill
    (read-only, or an own Skill of human origin) are violations whatever the
    Tool answers, and so is any failed call.
    """

    def __init__(self, case: Mapping[str, Any]) -> None:
        self._reads: set[tuple[str, str]] = set()
        self._protected = {
            str(skill["name"])
            for skill in case.get("skills", [])
            if skill.get("readonly") or skill.get("origin", "agent") != "agent"
        }
        self._name_excludes = [str(token) for token in case.get("name_excludes", [])]
        self.violations: list[str] = []
        self.mutations: list[dict[str, Any]] = []

    def before(self, name: str, arguments: Any, *, own_file_exists: bool) -> None:
        """Check one call before it runs; ``own_file_exists`` names its target file."""
        arguments = arguments if isinstance(arguments, dict) else {}
        action = str(arguments.get("action", ""))
        if (
            name == "memory"
            and action in MEMORY_MUTATIONS
            and ("memory", str(arguments.get("scope", ""))) not in self._reads
        ):
            self.violations.append("memory_write_without_current_list")
        if name != "skill_manage":
            return
        target = str(arguments.get("name", ""))
        file_path = str(arguments.get("file_path") or "SKILL.md")
        if action == "create":
            if ("skill", "catalog") not in self._reads:
                self.violations.append("create_without_current_catalog")
            if target and ticket_like_skill_name(target, self._name_excludes):
                self.violations.append("ticket_like_skill_name")
        elif own_file_exists and (target, file_path) not in self._reads:
            self.violations.append("skill_write_without_current_file")
        if target in self._protected:
            self.violations.append("protected_skill_write")

    def after(self, name: str, arguments: Any, result: Mapping[str, Any]) -> None:
        """Record one call's result: failures, mutations and what it let the Model read."""
        arguments = arguments if isinstance(arguments, dict) else {}
        action = str(arguments.get("action", ""))
        ok = bool(result.get("ok"))
        if name == "skill_manage" or (name == "memory" and action in MEMORY_MUTATIONS):
            self.mutations.append({"tool": name, "action": action, "ok": ok})
        if not ok:
            self.violations.append("tool_call_rejected")
            return
        if name == "memory" and action == "list":
            scope = arguments.get("scope")
            # A list without scope shows both scopes.
            self._reads.update(
                ("memory", item) for item in ((scope,) if scope else ("user", "agent"))
            )
        elif name == "skill" and not arguments.get("name"):
            self._reads.add(("skill", "catalog"))
        elif name == "skill" and arguments.get("file_path"):
            self._reads.add((str(arguments["name"]), str(arguments["file_path"])))


def _changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def _skill_name(path: str) -> str:
    parts = path.split("/")
    return parts[1] if len(parts) > 2 else ""


def _match(
    expected: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    mutations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    kind = expected["kind"]
    changed = _changed_files(before["files"], after["files"])
    memory_same = before["memory"] == after["memory"]
    payload = ""
    if kind == "none":
        effect_ok = memory_same and not changed and not mutations
    elif kind in ("user", "agent"):
        other = "agent" if kind == "user" else "user"
        payload = "\n".join(after["memory"][kind])
        effect_ok = (
            not changed
            and before["memory"][other] == after["memory"][other]
            and before["memory"][kind] != after["memory"][kind]
            and len(after["memory"][kind]) == expected.get("count", 1)
        )
    elif kind in ("create", "update"):
        names = {_skill_name(path) for path in changed}
        created = [
            path
            for path in changed
            if path.startswith("own/")
            and path.endswith("/SKILL.md")
            and path not in before["files"]
        ]
        effect_ok = (
            memory_same and len(names) == 1 and all(path.startswith("own/") for path in changed)
        )
        if kind == "create":
            effect_ok = effect_ok and len(created) == 1
            payload = "\n".join(after["files"].get(path, "") for path in changed)
        else:
            allowed = set(expected.get("names") or [expected["name"]])
            name = next(iter(names), "")
            effect_ok = effect_ok and not created and name in allowed
            payload = (
                after["files"].get(f"own/{name}/SKILL.md", "")
                + "\n"
                + "\n".join(after["files"].get(path, "") for path in changed)
            )
    else:
        raise ValueError(f"Unknown expectation kind: {kind}")
    lowered = payload.lower()
    evidence_ok = all(token.lower() in lowered for token in expected.get("contains", [])) and all(
        token.lower() not in lowered for token in expected.get("excludes", [])
    )
    return {"kind": kind, "effect_ok": effect_ok, "evidence_ok": evidence_ok}


def _written_sentences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Return the sentences an attempt added to Memory or to Skill files."""
    added: list[str] = []
    for scope, entries in after["memory"].items():
        old = set(before["memory"].get(scope, []))
        added.extend(entry for entry in entries if entry not in old)
    for path in _changed_files(before["files"], after["files"]):
        old_lines = set(before["files"].get(path, "").splitlines())
        added.extend(
            line for line in after["files"].get(path, "").splitlines() if line not in old_lines
        )
    return [
        sentence.strip()
        for text in added
        for sentence in _SENTENCE_SPLIT.split(text)
        if sentence.strip()
    ]


def _state_violations(
    case: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[str]:
    violations: list[str] = []

    def memory_text(state: Mapping[str, Any]) -> str:
        return "\n".join(entry for entries in state["memory"].values() for entry in entries).lower()

    def own_skill_text(state: Mapping[str, Any]) -> str:
        return "\n".join(
            text for path, text in state["files"].items() if path.startswith("own/")
        ).lower()

    for token in case.get("preference_tokens", []):
        lowered = str(token).lower()
        now = lowered in memory_text(after) and lowered in own_skill_text(after)
        earlier = lowered in memory_text(before) and lowered in own_skill_text(before)
        if now and not earlier:
            violations.append("duplicate_preference")
            break
    claims = [re.compile(pattern, re.IGNORECASE) for pattern in case.get("forbidden_claims", [])]
    if claims and any(
        claim.search(sentence) and not _CONDITIONAL.search(sentence)
        for sentence in _written_sentences(before, after)
        for claim in claims
    ):
        violations.append("forbidden_claim")
    return violations


def score_attempt(
    case: Mapping[str, Any],
    scope: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    observer: CallObserver,
    *,
    finished: bool,
) -> dict[str, Any]:
    """Score one attempt against the case's acceptable outcomes for ``scope``.

    An expectation is one outcome or ``{"any_of": [...]}``. Kinds: ``none`` (no
    write attempted, nothing changed), ``user``/``agent`` (only that Memory
    scope changed; ``count`` entries remain, default 1), ``create`` (exactly one
    new own Skill, Memory unchanged) and ``update`` (only an existing own Skill
    named by ``name`` or ``names`` changed, Memory unchanged). ``contains`` and
    ``excludes`` check the written text case-insensitively.
    """
    expectation = case["expected"][scope]
    alternatives = list(expectation.get("any_of") or [expectation])
    matches = [
        _match(alternative, before, after, observer.mutations) for alternative in alternatives
    ]
    matched = next(
        (
            index
            for index, match in enumerate(matches)
            if match["effect_ok"] and match["evidence_ok"]
        ),
        None,
    )
    violations = [*observer.violations, *_state_violations(case, before, after)]
    effect_passed = matched is not None
    return {
        "passed": finished and effect_passed and not violations,
        "effect_passed": effect_passed,
        "effect": {
            "expected": expectation,
            "matched_alternative": matched,
            "alternatives": matches,
            "memory_changed": before["memory"] != after["memory"],
            "changed_files": _changed_files(before["files"], after["files"]),
            "mutations": list(observer.mutations),
        },
        "violations": violations,
    }
