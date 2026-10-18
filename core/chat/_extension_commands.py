"""Extension Command registration authority and surface-neutral outcome validation."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import cast

from core.chat._command_types import (
    CommandArgumentMode,
    CommandCatalogResult,
    CommandExecutionMode,
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    CommandResourceChange,
    CommandRun,
    CommandSpec,
    CommandSurfaceKind,
    ExtensionCommandHandler,
    _RegisteredExtensionCommand,
)
from core.runs import (
    Run,
)
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN


class ExtensionCommandRegistry:
    """Keep one unambiguous live catalog and stable registration identities."""

    def __init__(self, builtin_names: Collection[str]) -> None:
        self._builtin_names = frozenset(builtin_names)
        self._commands: dict[str, _RegisteredExtensionCommand] = {}
        self._next_extension_registration_id = 1

    def get(self, name: str) -> _RegisteredExtensionCommand | None:
        return self._commands.get(name)

    def __getitem__(self, name: str) -> _RegisteredExtensionCommand:
        return self._commands[name]

    def specs(self) -> dict[str, CommandSpec]:
        return {name: registered.spec for name, registered in self._commands.items()}

    def register(
        self,
        extension_name: str,
        *,
        name: str,
        description: str,
        handler: ExtensionCommandHandler,
        argument: str = "optional",
        catalog_result: str = "notice",
        execution_mode: str = "serialized",
        argument_execution_mode: str | None = None,
        unavailable_surfaces: object = (),
        page_ids: frozenset[str] = frozenset(),
    ) -> int:
        """Validate and install one Extension-owned command.

        Runtime/ExtensionRegistry owns ordering and diagnostics. This seam still
        rejects every invalid or conflicting direct call so the dispatcher can
        never contain an ambiguous command table.
        """
        if not isinstance(extension_name, str) or not extension_name:
            raise ValueError("extension name must be a non-empty string")
        if (
            not isinstance(name, str)
            or name != name.lower()
            or SKILL_NAME_TRIGGER_PATTERN.fullmatch(name) is None
        ):
            raise ValueError(
                "name must be 1-64 lowercase letters, digits, hyphens, or underscores "
                "and start with a letter or digit"
            )
        if name in self._builtin_names:
            raise ValueError("a Built-in Command already uses this name")
        if name in self._commands:
            owner = self._commands[name].extension_name
            raise ValueError(f"name already registered by extension {owner!r}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")
        if not callable(handler):
            raise ValueError("handler must be callable")
        if not isinstance(page_ids, frozenset) or any(
            not isinstance(page_id, str) or not page_id for page_id in page_ids
        ):
            raise ValueError("page_ids must contain registered page identifiers")
        if not isinstance(argument, str) or argument not in {"none", "optional", "required"}:
            raise ValueError("argument must be one of: none, optional, required")
        if not isinstance(catalog_result, str) or catalog_result not in {
            "notice",
            "detail",
            "state_change",
        }:
            raise ValueError("catalog_result must be one of: notice, detail, state_change")
        if not isinstance(execution_mode, str) or execution_mode not in {
            "immediate",
            "serialized",
        }:
            raise ValueError("execution_mode must be one of: immediate, serialized")
        if argument_execution_mode is not None and (
            not isinstance(argument_execution_mode, str)
            or argument_execution_mode not in {"immediate", "serialized"}
        ):
            raise ValueError(
                "argument_execution_mode must be one of: immediate, serialized, or None"
            )
        if (
            isinstance(unavailable_surfaces, (str, bytes))
            or not isinstance(unavailable_surfaces, (tuple, list, set, frozenset))
            or any(not isinstance(surface, str) for surface in unavailable_surfaces)
        ):
            raise ValueError(
                "unavailable_surfaces must be a collection containing webui, tui or channel"
            )
        normalized_surfaces = frozenset(unavailable_surfaces)
        invalid_surfaces = normalized_surfaces - {"webui", "tui", "channel"}
        if invalid_surfaces:
            names = ", ".join(sorted(invalid_surfaces))
            raise ValueError(f"unavailable_surfaces contains unsupported values: {names}")

        registration_id = self._next_extension_registration_id
        self._next_extension_registration_id += 1
        self._commands[name] = _RegisteredExtensionCommand(
            spec=CommandSpec(
                name=name,
                description=description.strip(),
                argument=cast(CommandArgumentMode, argument),
                catalog_result=cast(CommandCatalogResult, catalog_result),
                execution_mode=cast(CommandExecutionMode, execution_mode),
                argument_execution_mode=cast(CommandExecutionMode | None, argument_execution_mode),
                unavailable_surfaces=cast(frozenset[CommandSurfaceKind], normalized_surfaces),
                # Extension commands work on a Session: a new one is created for them.
                new_session_mode="create_session",
            ),
            extension_name=extension_name,
            handler=handler,
            registration_id=registration_id,
            page_ids=page_ids,
        )
        return registration_id

    def unregister(self, extension_name: str) -> int:
        """Remove every live command owned by *extension_name*."""
        removed_names = [
            name
            for name, registered in self._commands.items()
            if registered.extension_name == extension_name
        ]
        for name in removed_names:
            del self._commands[name]
        return len(removed_names)


def validate_extension_outcome(
    result: object,
    *,
    expected_command: str,
    extension_name: str,
    page_ids: frozenset[str],
) -> None:
    """Keep malformed Extension values from escaping into surface projectors."""
    if not isinstance(result, CommandOutcome):
        raise TypeError("handler must return CommandOutcome")
    if result.command != expected_command:
        raise ValueError(
            f"handler returned command {result.command!r}; expected {expected_command!r}"
        )
    if result.feedback is not None and (
        not isinstance(result.feedback, CommandFeedback)
        or result.feedback.kind not in {"notice", "detail"}
        or not isinstance(result.feedback.text, str)
    ):
        raise TypeError("CommandOutcome.feedback must be valid CommandFeedback")
    if not isinstance(result.facts, Mapping) or any(
        not isinstance(key, str) for key in result.facts
    ):
        raise TypeError("CommandOutcome.facts must be a mapping with string keys")
    if result.navigation is not None and (
        not isinstance(result.navigation, CommandNavigation)
        or result.navigation.kind
        not in {"continue_in_session", "offer_session", "open_extension_page"}
        or not isinstance(result.navigation.agent_id, str)
        or not isinstance(result.navigation.session_id, str)
        or (
            result.navigation.project_id is not None
            and not isinstance(result.navigation.project_id, str)
        )
    ):
        raise TypeError("CommandOutcome.navigation must be valid CommandNavigation")
    navigation = result.navigation
    if navigation is not None:
        if navigation.kind == "open_extension_page":
            if (
                navigation.extension != extension_name
                or not isinstance(navigation.page, str)
                or navigation.page not in page_ids
                or not isinstance(navigation.route, str)
                or len(navigation.route) > 2048
                or navigation.route.startswith("/")
                or any(character in navigation.route for character in ("\\", "\0", ":"))
                or any(part in {".", ".."} for part in navigation.route.split("/"))
                or navigation.agent_id
                or navigation.session_id
                or navigation.project_id is not None
            ):
                raise TypeError("CommandOutcome.navigation must be valid CommandNavigation")
        elif navigation.extension is not None or navigation.page is not None or navigation.route:
            raise TypeError("CommandOutcome.navigation must be valid CommandNavigation")
    if not isinstance(result.runs, tuple) or any(
        not isinstance(command_run, CommandRun)
        or command_run.role not in {"primary", "follow_up"}
        or not isinstance(command_run.run, Run)
        for command_run in result.runs
    ):
        raise TypeError("CommandOutcome.runs must contain valid CommandRun values")
    if sum(command_run.role == "primary" for command_run in result.runs) > 1:
        raise TypeError("CommandOutcome may contain at most one primary CommandRun")
    if not isinstance(result.resource_changes, tuple) or any(
        not isinstance(change, CommandResourceChange)
        or not isinstance(change.kind, str)
        or not change.kind
        or not isinstance(change.scope, Mapping)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in change.scope.items()
        )
        for change in result.resource_changes
    ):
        raise TypeError(
            "CommandOutcome.resource_changes must contain valid CommandResourceChange values"
        )
