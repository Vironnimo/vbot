"""Match a Desktop wakeword transcript to an operator-configured Home Assistant call."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost
from core.model_tasks.decision_types import CHOICE_OPTION_LIMIT, DecisionError
from core.tools.call_syntax import same_json_value
from core.utils.workers import BoundedWorkerPool

from ._wakeword_store import COMMAND_SCHEMA, FILENAME, Command, edit_command, read_commands, wording

_QUESTION = (
    "Which single configured command expresses the entire spoken request, "
    "including its target, action, timing and conditions? "
    "Distinguish requests to act from state questions, negations and quoted commands. "
    "Choose none if the requested room, device, timing or any other condition "
    "is missing from the configured phrase and its aliases."
)
_NO_MATCH = "No single command matches the whole request, or the intended command is unclear."
_COVERAGE_QUESTION = {
    "type": "choice",
    "id": "scope",
    "instructions": (
        "Compare the spoken request with the configured phrases. What would be lost by "
        "replacing the request with the most suitable configured phrase? Treat polite wording "
        "and synonyms as equivalent."
    ),
    "criteria": {
        "nothing": (
            "The same immediate action or query with the same requested target and conditions. "
            "The speaker adds only conversational wording."
        ),
        "detail": (
            "A specific room, device, time, quantity, condition or another requested action "
            "is absent or different in that phrase."
        ),
        "intent": (
            "The request has a different purpose, is ambiguous or cannot be expressed by a "
            "single configured phrase."
        ),
    },
}
# A stalled semantic lookup must leave time for the ordinary Wakeword request.
_DECISION_SECONDS = 3.0
_DECISION_COMMAND_LIMIT = CHOICE_OPTION_LIMIT - 1


def _says(command: Command, key: str) -> bool:
    return any(key == wording(phrase) for phrase in (command["phrase"], *command["aliases"]))


def _read_catalog(path: Path) -> tuple[list[Command], str]:
    commands = read_commands(path)
    encoded = json.dumps(commands, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return commands, hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _selection(mode: str, index: int, text: str, catalog: str) -> str:
    # Comparison across the Tool policy lookup. It grants no execution authority.
    encoded = json.dumps([mode, index, text, catalog])
    return f"{mode}:{index}:{hashlib.sha256(encoded.encode('ascii')).hexdigest()}"


class WakewordCommands:
    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.io = BoundedWorkerPool(name="homeassistant-commands", max_workers=1)
        self.revision = 0

    async def start(self, host: ExtensionHost) -> None:
        self.host = host

    def close(self) -> None:
        self.host = None
        self.io.shutdown()

    def _path(self) -> Path:
        if self.host is None or self.host.state_dir is None:
            raise ValueError("Home Assistant command storage is unavailable.")
        return self.host.state_dir / FILENAME

    def register(self) -> None:
        operations = self.api.operations
        operations.startup.append(self.start)
        self.api.on_shutdown(self.close)
        operations.register(
            "wakeword_commands",
            "List configured Home Assistant wakeword commands.",
            {"type": "object", "additionalProperties": False},
            self.list_commands,
        )
        nullable_command = {"anyOf": [COMMAND_SCHEMA, {"type": "null"}]}
        operations.register(
            "set_wakeword_command",
            "Add, edit or remove one Home Assistant wakeword command.",
            {
                "type": "object",
                "properties": {"previous": nullable_command, "command": nullable_command},
                "required": ["previous", "command"],
                "additionalProperties": False,
            },
            self.set_command,
        )
        operations.register(
            "match_wakeword",
            "Select the configured Home Assistant command for a wakeword transcript.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            self.match,
        )
        operations.register(
            "confirm_wakeword",
            "Check whether a selected wakeword call still matches the current configuration.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "selection": {"type": "string", "maxLength": 100},
                },
                "required": ["text", "selection"],
                "additionalProperties": False,
            },
            self.confirm,
        )

    async def list_commands(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {
            "commands": await self.io.run(read_commands, self._path()),
            "decision_command_limit": _DECISION_COMMAND_LIMIT,
        }

    async def set_command(self, request: dict[str, Any]) -> dict[str, Any]:
        previous, command = request["previous"], request["command"]
        if command is not None:
            registry = self.api.operations.tool_registry
            if registry is None:
                raise ValueError("Home Assistant Tools are unavailable.")
            tool = registry.get(command["tool"])
            if tool.extension != "homeassistant":
                raise ValueError("This Tool is unavailable to Home Assistant.")
            arguments = command["arguments"]
            if tool.argument_normalizer is not None:
                arguments = tool.argument_normalizer(arguments)
            tool.contract.validate_arguments(arguments, unadvertised=tool.unadvertised_parameters)
        commands = await self.io.run(edit_command, self._path(), previous, command)
        if not same_json_value(previous, command):
            self.api.logger.info(
                "Home Assistant wakeword commands updated (actor=operator count=%d)", len(commands)
            )
            self.revision += 1
            if self.host is not None and self.host.publish_change is not None:
                self.host.publish_change("wakeword_commands", [], self.revision)
        return {"commands": commands, "decision_command_limit": _DECISION_COMMAND_LIMIT}

    async def match(self, request: dict[str, Any]) -> dict[str, Any]:
        text = request["text"]
        key = wording(text)
        if not key or not self.api.resolve_credential("HASS_TOKEN").strip():
            return {"matched": False}
        commands, catalog = await self.io.run(_read_catalog, self._path())
        config = self.api.get_config()
        for index, command in enumerate(commands):
            if _says(command, key):
                return {
                    **self._matched(command, config),
                    "selection": _selection("exact", index, text, catalog),
                }
        if (
            not 0 < len(commands) <= _DECISION_COMMAND_LIMIT
            or config.get("wakeword_use_decision", True) is not True
        ):
            return {"matched": False}
        host = self.host
        if host is None or host.decide is None:
            return {"matched": False}
        # Labels are opaque and local to this request. Fixed Tool arguments (which can include
        # credentials or message content) stay on the server even when the Model is remote.
        choices = {f"c{index + 1}": index for index in range(len(commands))}
        state = {
            "transcript": text,
            "configured_phrases": [[c["phrase"], *c["aliases"]] for c in commands],
        }
        criteria = {label: commands[index]["phrase"] for label, index in choices.items()}
        criteria["none"] = _NO_MATCH
        question = {
            "id": "command",
            "type": "choice",
            "instructions": _QUESTION,
            "criteria": criteria,
        }
        registry = self.api.operations.tool_registry
        if registry is None:
            return {"matched": False}
        masker = registry.secret_masker
        try:
            async with asyncio.timeout(_DECISION_SECONDS):
                decision = await host.decide(
                    None,
                    masker.mask_unbound(
                        {"state": state, "questions": [question, _COVERAGE_QUESTION]}
                    ),
                )
        except (DecisionError, TimeoutError) as exc:
            reason = exc.code if isinstance(exc, DecisionError) else "timeout"
            self.api.logger.debug("Wakeword decision unavailable (reason=%s)", reason)
            return {"matched": False}
        answers = decision.get("answers", {})
        coverage = answers.get("scope", {})
        if coverage.get("type") != "choice" or coverage.get("choice") != "nothing":
            return {"matched": False}
        answer = answers.get("command", {})
        selected_index = (
            choices.get(answer.get("choice")) if answer.get("type") == "choice" else None
        )
        if selected_index is None or self.host is not host:
            return {"matched": False}
        selected = commands[selected_index]
        # A bare question like "Light on?" needs its own intent. It can select a
        # state query, or an explicitly configured question can match exactly.
        if (
            key.endswith("?")
            and selected["tool"] == "ha_call_service"
            and _says(selected, wording(key.rstrip("?")))
        ):
            return {"matched": False}
        return {
            **self._matched(selected, config),
            "selection": _selection("decision", selected_index, text, catalog),
        }

    async def confirm(self, request: dict[str, Any]) -> dict[str, Any]:
        ticket = request["selection"]
        parts = re.fullmatch(r"(exact|decision):([0-9]+):[0-9a-f]{64}", ticket)
        if parts is None:
            return {"matched": False}
        mode, offset = parts.groups()
        index = int(offset)
        commands, catalog = await self.io.run(_read_catalog, self._path())
        config = self.api.get_config()
        if (
            index >= len(commands)
            or _selection(mode, index, request["text"], catalog) != ticket
            or (mode == "decision" and config.get("wakeword_use_decision", True) is not True)
            or not self.api.resolve_credential("HASS_TOKEN").strip()
        ):
            return {"matched": False}
        return self._matched(commands[index], config)

    @staticmethod
    def _matched(command: Command, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "matched": True,
            "tool": command["tool"],
            "arguments": command["arguments"],
            "notify_agent": config.get("wakeword_notify_agent", False) is True,
        }
