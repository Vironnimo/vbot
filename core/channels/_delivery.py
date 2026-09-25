"""Internal outbound validation, Run-button binding and completion relay."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from core.channels.adapter import (
    FileData,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBinding,
    bound_run_callback_data,
)
from core.chat.messages import ReplySurface
from core.extensions import InteractionButton
from core.sessions import SessionAddress
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.runs import Run

from core.channels.config import (
    ChannelConfigError,
    _normalize_channel_id,
)

if TYPE_CHECKING:
    from core.channels.channels import ChannelService

_LOGGER = get_logger("channels")

_MAX_CALLBACK_DATA_BYTES = 64


def _normalize_outbound_buttons(
    buttons: list[list[InteractionButton]] | None,
) -> list[list[InteractionButton]] | None:
    """Validate an outbound inline-keyboard before it reaches an adapter.

    Returns ``None`` when no buttons were given (or every row is empty). Raises
    :class:`ChannelConfigError` on a malformed structure or a button whose
    callback ``data`` is empty or exceeds the 64-byte Telegram limit — so a bad
    payload fails at the service boundary, uniformly across adapters, rather than
    deep inside the Bot API call.
    """
    if buttons is None:
        return None
    if not isinstance(buttons, list):
        raise ChannelConfigError("buttons must be a list of button rows when provided")

    normalized: list[list[InteractionButton]] = []
    for row in buttons:
        if not isinstance(row, list):
            raise ChannelConfigError("each button row must be a list of buttons")
        normalized_row: list[InteractionButton] = []
        for button in row:
            if not isinstance(button, InteractionButton):
                raise ChannelConfigError("buttons must contain InteractionButton values only")
            if not isinstance(button.label, str) or not button.label:
                raise ChannelConfigError("each button label must be a non-empty string")
            if not isinstance(button.data, str) or not button.data:
                raise ChannelConfigError("each button data must be a non-empty string")
            if len(button.data.encode("utf-8")) > _MAX_CALLBACK_DATA_BYTES:
                raise ChannelConfigError(
                    f"button data exceeds {_MAX_CALLBACK_DATA_BYTES} bytes: {button.data!r}"
                )
            normalized_row.append(button)
        if normalized_row:
            normalized.append(normalized_row)

    return normalized or None


def _bind_outbound_run_buttons(
    rows: list[list[InteractionButton]],
    *,
    platform_target: str,
    thread_id: str | None,
    origin_session_id: str,
) -> tuple[list[list[InteractionButton]], RunButtonBinding | None]:
    binding_id = uuid4().hex
    original_data: list[str] = []
    bound_rows: list[list[InteractionButton]] = []
    for row in rows:
        bound_row: list[InteractionButton] = []
        for button in row:
            if button.data.split(":", 1)[0] != "run":
                bound_row.append(button)
                continue
            button_index = len(original_data)
            original_data.append(button.data)
            bound_row.append(
                InteractionButton(
                    label=button.label,
                    data=bound_run_callback_data(binding_id, button_index),
                )
            )
        bound_rows.append(bound_row)
    if not original_data:
        return rows, None
    return bound_rows, RunButtonBinding(
        id=binding_id,
        platform_target=platform_target,
        thread_id=thread_id,
        origin_session_id=origin_session_id,
        original_button_data=tuple(original_data),
        created_at=utc_now_timestamp(),
    )


async def send(
    service: ChannelService,
    channel_id: str,
    message: str | None,
    platform_target: str,
    *,
    files: list[FileData] | None = None,
    thread_id: str | None = None,
    buttons: list[list[InteractionButton]] | None = None,
    run_origin: RouteFacts | None = None,
) -> None:
    """Delegate an outbound send to a running channel adapter."""
    normalized_id = _normalize_channel_id(channel_id)
    if not isinstance(platform_target, str) or not platform_target:
        raise ChannelConfigError("platform_target must be a non-empty string")
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
        raise ChannelConfigError("thread_id must be a non-empty string when provided")

    normalized_message: str | None
    if message is None:
        normalized_message = None
    elif isinstance(message, str) and message.strip():
        normalized_message = message.strip()
    else:
        raise ChannelConfigError("message must be a non-empty string when provided")

    normalized_files: list[FileData] | None
    if files is None:
        normalized_files = None
    elif not isinstance(files, list):
        raise ChannelConfigError("files must be a list of FileData when provided")
    else:
        normalized_files = []
        for file_data in files:
            if not isinstance(file_data, FileData):
                raise ChannelConfigError("files must contain FileData values only")
            normalized_files.append(file_data)

    normalized_buttons = _normalize_outbound_buttons(buttons)
    if run_origin is not None and not isinstance(run_origin, RouteFacts):
        raise ChannelConfigError("run_origin must be RouteFacts when provided")

    if normalized_message is None and not normalized_files:
        raise ChannelConfigError("at least one of message or files must be provided")

    saved: list[RunButtonBinding] = []
    try:
        adapter = service._active_adapter(normalized_id)
        outbound_buttons = normalized_buttons
        if run_origin is not None and normalized_buttons is not None:
            # One hop per database: the origin check on the Session database's
            # pool, then the ownership check and the binding it authorizes as one
            # unit on the Channel state's pool.
            chat_sessions = service._chat_sessions
            origin = SessionAddress(
                project_id=None, agent_id=run_origin.agent_id, session_id=run_origin.session_id
            )
            if not await chat_sessions.run_async(chat_sessions.exists, origin):
                raise ChannelConfigError(
                    f"Run-button origin Session does not exist: {run_origin.session_id}"
                )
            outbound_buttons = await service._state.run_async(
                _bind_run_buttons,
                service,
                normalized_id,
                normalized_buttons,
                platform_target=platform_target,
                thread_id=thread_id,
                run_origin=run_origin,
                saved=saved,
            )
        await adapter.send(
            normalized_message,
            platform_target,
            files=normalized_files,
            thread_id=thread_id,
            buttons=outbound_buttons,
        )
    except BaseException:
        if saved:
            await _discard_unsent_binding(service, normalized_id, saved[0])
        raise


async def _discard_unsent_binding(
    service: ChannelService, channel_id: str, binding: RunButtonBinding
) -> None:
    """Remove a binding whose message was not sent, even under repeated cancellation."""
    cleanup = asyncio.create_task(
        service._state.run_async(service._state.discard_run_button_binding, channel_id, binding.id)
    )
    cancelled = False
    try:
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
    except Exception as cleanup_error:
        _LOGGER.warning(
            "Could not discard unsent Run-button binding (channel=%s): %s",
            channel_id,
            cleanup_error,
            exc_info=(
                type(cleanup_error),
                cleanup_error,
                cleanup_error.__traceback__,
            ),
        )
    if cancelled:
        raise asyncio.CancelledError from None


async def relay_completion_run(
    service: ChannelService, run: Run, reply_surface: ReplySurface
) -> None:
    """Relay a background completion Run to its Session's latest Channel target."""
    if reply_surface.kind != "channel" or reply_surface.channel_id is None:
        return
    address = SessionAddress(
        project_id=run.project_id,
        agent_id=run.agent_id,
        session_id=run.session_id,
    )
    metadata = await service._chat_sessions.get_metadata_async(address)
    raw_target = metadata.get("last_reply_target")
    if not isinstance(raw_target, dict):
        raise ChannelConfigError(f"Session has no Channel reply target: {run.session_id}")
    channel_id = raw_target.get("channel_id")
    platform_target = raw_target.get("platform_target")
    thread_id = raw_target.get("thread_id")
    if channel_id != reply_surface.channel_id:
        raise ChannelConfigError(
            f"Session reply target does not match Channel {reply_surface.channel_id}"
        )
    if not isinstance(platform_target, str) or not platform_target:
        raise ChannelConfigError("Session Channel reply target is invalid")
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
        raise ChannelConfigError("Session Channel thread target is invalid")

    config = service._storage.get(channel_id)
    if config.agent_id != run.agent_id or config.platform != reply_surface.platform:
        raise ChannelConfigError(f"Session reply surface no longer matches Channel {channel_id}")
    adapter = service._active_adapter(channel_id)
    await adapter.relay_run(
        run,
        ReplyPlanFacts(
            channel_id=channel_id,
            platform_target=platform_target,
            thread_id=thread_id,
        ),
    )


def _bind_run_buttons(
    service: ChannelService,
    channel_id: str,
    buttons: list[list[InteractionButton]],
    *,
    platform_target: str,
    thread_id: str | None,
    run_origin: RouteFacts,
    saved: list[RunButtonBinding],
) -> list[list[InteractionButton]]:
    """Check that the origin Agent owns the Channel, then persist its Run-button binding."""
    config = service._storage.get(channel_id)
    if run_origin.agent_id != config.agent_id:
        raise ChannelConfigError(
            f"Run-button origin agent {run_origin.agent_id} does not own Channel {channel_id}"
        )
    outbound_buttons, binding = _bind_outbound_run_buttons(
        buttons,
        platform_target=platform_target,
        thread_id=thread_id,
        origin_session_id=run_origin.session_id,
    )
    if binding is not None:
        service._state.save_run_button_binding(channel_id, binding)
        # Recorded inside the worker: cancellation drains the worker but does not
        # return its result, and the caller must still discard the unsent binding.
        saved.append(binding)
    return outbound_buttons
