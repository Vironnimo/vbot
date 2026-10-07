"""The Terminals layout of a Live call: groups, order, maximizing and closing.

Changes go through the canonical ``terminal.*`` RPCs; the app window then
refreshes its Terminals view through a ``terminal_view`` UI request. Only
groups the user or an Agent created can be changed.
"""

from __future__ import annotations

import logging
from typing import Any

from core.model_tasks.live import live_failure, live_success
from core.tools.live import MAX_LIVE_NAME_CHARS
from server.live._context import (
    VOICE_STOPPED,
    JsonObject,
    LiveContext,
    LiveToolError,
    LiveUiError,
    count_phrase,
    join_words,
    text_field,
)
from server.live._targets import (
    GROUP,
    TERMINAL,
    LiveCatalog,
    LiveRefs,
    Target,
    resolve_target,
    terminal_title,
)
from server.rpc.errors import RpcError

_LOGGER = logging.getLogger("vbot.server.live")

EDITABLE_GROUP_KINDS = frozenset({"user", "agent"})
FINISHED_STATES = frozenset({"exited", "error"})
# The Terminal manager's state while the program in a Terminal is busy.
WORKING = "working"


async def action_target(
    arguments: JsonObject, action: str, kinds: set[str], refs: LiveRefs, catalog: LiveCatalog
) -> Target:
    """The target a ``terminal`` action names; every action but a few needs one."""
    target_text = text_field(arguments, "target")
    if not target_text:
        raise LiveToolError(
            "missing_target",
            f"{action} needs a target. Call manage_terminals again with "
            f'{{"action": "{action}", "target": '
            f'"{"<group name>" if action.endswith("_group") else "t1"}"}}.',
        )
    return await resolve_target(
        target_text, kinds, tool="terminal", field="target", refs=refs, catalog=catalog
    )


class LiveTerminalLayout:
    """Run the layout actions of the ``terminal`` Tool for one call."""

    def __init__(self, ctx: LiveContext, refs: LiveRefs) -> None:
        self._ctx = ctx
        self._refs = refs

    async def run(self, action: str, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        """Run one layout action: everything but ``key``."""
        if action == "restore":
            await self._ctx.view("restore")
            return live_success("Restored the Terminals view to its group layout.")
        if action == "create_group":
            return await self._create_group(text_field(arguments, "name"))
        if action == "reorder":
            return await self._reorder(arguments, catalog)
        if action in {"rename_group", "delete_group"}:
            target = await action_target(arguments, action, {GROUP}, self._refs, catalog)
            group = target.group
            assert group is not None
            if action == "delete_group" and arguments.get("confirm") is not True:
                working = [
                    self._refs.terminal(str(item["terminal_id"]), terminal_title(item))
                    for item in await catalog.terminals()
                    if item.get("group_id") == group["group_id"] and item.get("state") == WORKING
                ]
                if working:
                    label = str(group.get("name") or group["group_id"])
                    raise LiveToolError(
                        "terminal_working",
                        f'The group "{label}" has Terminals that are still working: '
                        f"{join_words(working)}. Deleting the group stops them and breaks off "
                        "that work, for example a task or an update. Ask the user whether to "
                        "delete it anyway; only if they agree, call manage_terminals again with "
                        f'{{"action": "delete_group", "target": "{label}", "confirm": true}}.',
                    )
            return await self._group_change(action, group, text_field(arguments, "name"))
        target = await action_target(arguments, action, {TERMINAL}, self._refs, catalog)
        terminal = target.terminal
        assert terminal is not None
        ref = self._refs.terminal(str(terminal["terminal_id"]), terminal_title(terminal))
        if action == "maximize":
            await self._ctx.view("maximize", terminal_id=terminal["terminal_id"])
            return live_success(f"Maximized {ref} in the Terminals view.")
        if terminal.get("state") == WORKING and arguments.get("confirm") is not True:
            raise LiveToolError(
                "terminal_working",
                f"{ref} is still working. Closing it stops it and breaks off that work, for "
                "example a task or an update. Ask the user whether to close it anyway; only if "
                "they agree, call manage_terminals again with "
                f'{{"action": "close", "target": "{ref}", "confirm": true}}.',
            )
        return await self._close(terminal, ref)

    async def _create_group(self, name: str) -> JsonObject:
        if not name or len(name) > MAX_LIVE_NAME_CHARS:
            raise LiveToolError(
                "invalid_name",
                f"create_group needs a name of at most {MAX_LIVE_NAME_CHARS} characters. Call "
                'manage_terminals again with {"action": "create_group", "name": "<group name>"}.',
            )
        created = await self._ctx.call("terminal.group.create", {"name": name})
        text = f'Created the Terminal group "{name}".'
        shown = await self.show_quietly("show_group", group_id=created["group"]["group_id"])
        return live_success(text if shown else f"{text} The app did not switch to it.")

    async def _group_change(self, action: str, group: JsonObject, name: str) -> JsonObject:
        label = str(group.get("name") or group["group_id"])
        if group.get("kind") not in EDITABLE_GROUP_KINDS:
            raise LiveToolError(
                "group_not_editable",
                f'The group "{label}" is kept by the app and cannot be changed; only groups the '
                "user or an Agent created can.",
            )
        if action == "rename_group":
            if not name or len(name) > MAX_LIVE_NAME_CHARS:
                raise LiveToolError(
                    "invalid_name",
                    f"rename_group needs a new name of at most {MAX_LIVE_NAME_CHARS} characters. "
                    f'Call manage_terminals again with {{"action": "rename_group", "target": '
                    f'"{label}", '
                    '"name": "<new name>"}.',
                )
            await self._ctx.call(
                "terminal.group.rename", {"group_id": group["group_id"], "name": name}
            )
            text = f'Renamed the group "{label}" to "{name}".'
        else:
            result = await self._ctx.call("terminal.group.delete", {"group_id": group["group_id"]})
            killed = result.get("terminals_killed")
            stopped = (
                f" and stopped {count_phrase(killed, 'Terminal')}"
                if isinstance(killed, int) and killed
                else ""
            )
            text = f'Deleted the group "{label}"{stopped}.'
        return live_success(await self._after_change(text))

    async def _reorder(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        order = arguments.get("order")
        if (
            not isinstance(order, list)
            or not order
            or not all(isinstance(item, str) for item in order)
        ):
            raise LiveToolError(
                "missing_order",
                "reorder needs order: every Terminal ref of the group in the new order. Call "
                'manage_terminals again with {"action": "reorder", "order": ["t2", "t1"]}.',
            )
        terminals: list[JsonObject] = []
        for item in order:
            target = await resolve_target(
                item, {TERMINAL}, tool="terminal", field="order", refs=self._refs, catalog=catalog
            )
            assert target.terminal is not None
            terminals.append(target.terminal)
        target_text = text_field(arguments, "target")
        if target_text:
            target = await resolve_target(
                target_text,
                {GROUP},
                tool="terminal",
                field="target",
                refs=self._refs,
                catalog=catalog,
            )
            group = target.group
        else:
            group_ids = {item.get("group_id") for item in terminals}
            groups = await catalog.groups()
            group = next(
                (item for item in groups if len(group_ids) == 1 and item["group_id"] in group_ids),
                None,
            )
        if group is None:
            raise LiveToolError(
                "missing_group",
                "The Terminals are in different groups. Call manage_terminals again with the "
                "group name "
                'as target, for example {"action": "reorder", "target": "Codex", "order": '
                '["t2", "t1"]}.',
            )
        label = str(group.get("name") or group["group_id"])
        if group.get("kind") not in EDITABLE_GROUP_KINDS:
            raise LiveToolError(
                "group_not_editable",
                f'The group "{label}" is kept by the app and cannot be reordered.',
            )
        members = [
            item for item in await catalog.terminals() if item.get("group_id") == group["group_id"]
        ]
        ids = [str(item["terminal_id"]) for item in terminals]
        member_ids = {str(item["terminal_id"]) for item in members}
        if len(ids) != len(member_ids) or set(ids) != member_ids:
            refs = [
                self._refs.terminal(str(item["terminal_id"]), terminal_title(item))
                for item in members
            ]
            raise LiveToolError(
                "invalid_order",
                f'order must name every Terminal of the group "{label}" exactly once: '
                f"{', '.join(refs)}. Call manage_terminals again with all of them in the new "
                "order.",
            )
        await self._ctx.call("terminal.group.order", {"group_id": group["group_id"], "order": ids})
        new_order = ", ".join(
            self._refs.terminal(str(item["terminal_id"]), terminal_title(item))
            for item in terminals
        )
        return live_success(
            await self._after_change(f'Reordered the group "{label}": {new_order}.')
        )

    async def _close(self, terminal: JsonObject, ref: str) -> JsonObject:
        """Stop the Terminal, then forget it, like the app's close button."""
        terminal_id = terminal["terminal_id"]
        stopped = terminal.get("state") in FINISHED_STATES
        try:
            if not stopped:
                await self._ctx.call("terminal.kill", {"terminal_id": terminal_id})
                stopped = True
            self._ctx.ensure_active()
            await self._ctx.call("terminal.forget", {"terminal_id": terminal_id})
        except Exception as exc:
            if not isinstance(exc, LiveToolError | RpcError):
                _LOGGER.exception("Live Terminal close failed unexpectedly")
            reason = exc.message if isinstance(exc, LiveToolError | RpcError) else "It failed."
            if not stopped:
                done = f"{ref} may still be running: stopping it failed."
            elif isinstance(exc, LiveToolError) and exc.code == VOICE_STOPPED:
                done = f"{ref} was stopped but not removed."
            else:
                done = f"{ref} was stopped but may not have been removed."
            return live_failure(
                "partial", f"{done} {reason} Call overview to check before closing it again."
            )
        return live_success(await self._after_change(f"Closed {ref}: stopped and removed."))

    async def show_quietly(self, op: str, **args: Any) -> bool:
        """Change the Terminals layout; ``False`` when the app did not."""
        try:
            self._ctx.ensure_active()
            await self._ctx.view(op, **args)
        except LiveToolError, LiveUiError:
            return False
        except Exception:
            _LOGGER.exception("Live Terminal layout update failed unexpectedly")
            return False
        return True

    async def _after_change(self, text: str) -> str:
        """Refresh the Terminals layout after a completed change; the change stays reported."""
        if await self.show_quietly("refresh"):
            return text
        return f"{text} The app window did not update its Terminals view."
