"""Execute one configured Home Assistant Tool on behalf of a Desktop wakeword input."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat.messages import ChatMessage
from core.chat.wire_shaping import quote_external_value
from core.sessions import SessionAddress
from core.tools import ToolContext, resolve_tool_access, tool_failure_for_exception
from core.utils.ids import new_id
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime
    from core.sessions import ChatSession

_OWNER = "homeassistant"
_MATCH = "match_wakeword"
_LOGGER = get_logger("wakeword")


@dataclass(frozen=True)
class WakewordResult:
    ok: bool
    message: str
    reminder: ChatMessage | None = None
    session: ChatSession | None = None
    agent_created_at: str = ""


def _prepare(
    runtime: Runtime, agent_id: str, project_id: str | None, session_id: str | None, tool: str
) -> tuple[ToolContext, tuple[str, ...], ChatSession | None, str]:
    """Read the wakeword target's current Tool policy without admitting a Chat Run."""
    address = SessionAddress(project_id, agent_id, session_id) if session_id else None
    session = runtime.chat_sessions.get(address) if address else None
    resolver = runtime.agent_resolver
    agent = resolver.resolve_agent(project_id, agent_id, session_id=session_id, require_model=False)
    working_project = resolver.resolve_working_project(project_id, agent, session_id=session_id)
    root = Path(runtime.projects.get(working_project).cwd) if working_project else None
    ceiling = resolver.session_overrides(address).ceiling() if address else None
    access = resolve_tool_access(
        agent.tool_access,
        runtime.tools.list_tools(),
        agent.memory_prompt_mode,
        workspace=agent.workspace,
        project_memory_enabled=agent.project_memory_enabled,
    )
    context = ToolContext(
        agent_id=agent_id,
        project_id=project_id,
        session_id=session_id or "",
        session_generation_id=session.generation_id if session else None,
        run_id="",
        tool_call_id=new_id("call"),
        tool_call_index=0,
        tool_name=tool,
        workspace=Path(agent.workspace),
        cwd=root or Path(agent.workspace),
        working_project_id=working_project,
        data_root=runtime.storage.data_dir,
        vbot_root=Path(__file__).resolve().parents[2],
        permissions=runtime._session_boundary(agent, root, ceiling, address),
    )
    return context, access.allowed_tools, session, agent.created_at


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _quoted(value: Any) -> str:
    quoted = quote_external_value(value)
    if len(quoted) <= 4000:
        return quoted
    return quote_external_value(_clip(quoted, 3999))


async def execute_wakeword(
    runtime: Runtime, text: str, agent_id: str, project_id: str | None, session_id: str | None
) -> WakewordResult | None:
    registry = runtime.extensions
    if registry is None:
        return None
    try:
        identity = registry.registration_identity(_OWNER)
        management = registry.management(_OWNER)
    except ValueError:
        return None
    if _MATCH not in management.operations:
        return None
    # Matching is read-only. The only invocation with side effects is dispatch below.
    try:
        match = await management.invoke(_MATCH, {"text": text})
    except Exception as error:
        _LOGGER.warning("Wakeword matcher unavailable (%s)", type(error).__name__)
        return None
    if not match.get("matched"):
        return None
    tool = match["tool"]
    arguments = match["arguments"]
    context, allowed, session, created_at = await runtime.chat_sessions.run_async(
        _prepare, runtime, agent_id, project_id, session_id, tool
    )
    # Agent/Session resolution awaited a worker. Re-read the selected action and both
    # switches before dispatch, without asking the Decision Model a second time.
    try:
        match = await management.invoke(
            "confirm_wakeword", {"text": text, "selection": match["selection"]}
        )
    except Exception as error:
        _LOGGER.warning("Wakeword command could not be confirmed (%s)", type(error).__name__)
        return None
    if not match.get("matched") or match["tool"] != tool or match["arguments"] != arguments:
        return None
    if runtime.extensions is not registry or not registry.is_service_registration_current(identity):
        return None
    try:
        registered = runtime.tools.get(tool)
        if registered.extension != _OWNER or registered.internal or registered.session_scoped:
            return WakewordResult(False, "The configured Home Assistant Tool is unavailable.")
        result = await runtime.tools.dispatch(context, arguments, allowed_tools=allowed)
    except Exception as error:
        result = tool_failure_for_exception(tool, error)
    masker = runtime.tools.secret_masker
    ok = result["ok"]
    data = masker.mask_unbound(result["data"] if ok else result["error"])
    if ok:
        parts = [str(data[key]) for key in ("note", "content", "response") if data.get(key)]
        message = "\n".join(parts) if parts else quote_external_value(data)
    else:
        message = data["message"]
    reminder = None
    if match["notify_agent"]:
        public_args = masker.mask_unbound(context.public_arguments or arguments)
        reminder = ChatMessage.note(
            "Home Assistant wakeword command processed. The recorded Tool call and its outcome "
            "follow as quoted data.\n"
            f"Tool: {quote_external_value(tool)}\nArguments: {_quoted(public_args)}\n"
            f"{'Result' if ok else 'Error'}: {_quoted(data)}"
        )
    _LOGGER.debug("Wakeword command finished (tool=%s ok=%s)", tool, ok)
    return WakewordResult(ok, _clip(message, 2000), reminder, session, created_at)
