"""The Live Tools: how the Agents of a Live voice call operate vBot.

A Live call has two built-in Agents (``core.agents``): the voice Agent, whose
Model talks with the user, and the backend Agent, which answers the requests
the voice Model hands on through ``vbot_request``. Both reach the app through
these Tools. They only organize work: look at what runs, start and steer Agents
and coding agents, and show things in the app.

The Tools work only while a call runs: the call binds the Sessions of both
Agents to its :class:`LiveToolHost` in :class:`LiveToolHosts`, and a handler
finds the host by the calling Session. The host (``server/live``) prepares the
arguments as the Model wrote them, runs the call, and returns a Tool result
envelope whose success data is one short plain-text ``content``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from core.tools._tool_results import tool_failure
from core.tools.availability import TOOL_CONSTRAINT_LIVE_CALL
from core.tools.tools import ToolContext, ToolDisplay, ToolDisplayField, ToolRegistry

JsonObject = dict[str, Any]

TOOL_OVERVIEW = "overview"
TOOL_START_AGENT_SESSION = "start_agent_session"
TOOL_START_CODING_TERMINAL = "start_coding_terminal"
TOOL_SEND_MESSAGE = "send_message"
TOOL_READ_OUTPUT = "read_output"
TOOL_STOP = "stop"
TOOL_OPEN = "open"
TOOL_MANAGE_TERMINALS = "manage_terminals"
TOOL_END_CALL = "end_call"
LIVE_TOOL_NAMES = (
    TOOL_OVERVIEW,
    TOOL_START_AGENT_SESSION,
    TOOL_START_CODING_TERMINAL,
    TOOL_SEND_MESSAGE,
    TOOL_READ_OUTPUT,
    TOOL_STOP,
    TOOL_OPEN,
    TOOL_MANAGE_TERMINALS,
    TOOL_END_CALL,
)
# Live Tools that only look; every other Live Tool can change something.
LIVE_READ_ONLY_TOOLS = frozenset({TOOL_OVERVIEW, TOOL_READ_OUTPUT})
# The voice Agent's line to the backend Agent; offered only while a call uses it.
TOOL_VBOT_REQUEST = "vbot_request"
LIVE_TOOL_FAMILY = "live"

# The coding programs start_coding_terminal starts (``server/live/_programs.py``).
LIVE_CODING_PROGRAMS = ("codex", "claude")
MAX_LIVE_COUNT = 10
MAX_LIVE_TEXT_CHARS = 16_000
MAX_LIVE_NAME_CHARS = 80
# Views open can show a thing in, by the kind of thing.
TARGET_VIEWS = ("chat", "terminals", "agents", "projects")
# Every view open can switch to.
LIVE_VIEWS = (*TARGET_VIEWS, "calendar", "cron", "skills", "settings", "statistics", "logs")
LIVE_KEYS = ("enter", "escape", "tab", "up", "down", "left", "right", "ctrl-c")
LIVE_TERMINAL_ACTIONS = (
    "maximize",
    "restore",
    "key",
    "close",
    "reorder",
    "create_group",
    "rename_group",
    "delete_group",
)

# Agent-facing: the result of a Live Tool called while no call runs.
_NO_CALL = (
    "This Tool works only during a Live voice call, and none is running for this Session. "
    "Nothing was done."
)


class LiveToolHost(Protocol):
    """The running call a Session's Live Tool calls reach.

    ``run_live_tool`` runs one Live Tool by its name with the arguments as the
    Model wrote them and never raises for operation failures: they come back
    as a failure envelope naming the next valid call. ``vbot_request`` answers
    one request of the voice Agent; *request* is ``None`` when the Model sent
    none, and the answer is then taken from the conversation.
    """

    async def run_live_tool(self, name: str, arguments: Any) -> JsonObject: ...

    async def vbot_request(self, request: str | None) -> JsonObject: ...


class LiveToolHosts:
    """Which running call each Session's Live Tool calls reach."""

    def __init__(self) -> None:
        self._hosts: dict[str, LiveToolHost] = {}

    def bind(self, session_id: str, host: LiveToolHost) -> None:
        self._hosts[session_id] = host

    def unbind(self, session_id: str, host: LiveToolHost) -> None:
        """Forget *session_id*'s host, unless another call bound it since."""
        if self._hosts.get(session_id) is host:
            del self._hosts[session_id]

    def get(self, session_id: str) -> LiveToolHost | None:
        return self._hosts.get(session_id)


# Agent-facing: one-line summaries for Tools whose description's first sentence
# is too long to list them by.
_SUMMARIES = {
    TOOL_OVERVIEW: (
        "Show what is going on in vBot: Agents, Projects, running and recently finished "
        "Sessions, and coding Terminals."
    ),
    TOOL_VBOT_REQUEST: (
        "Hand one request to vBot, which operates the app for the user; its result arrives "
        "later as this Tool's output."
    ),
}

# A Live Tool row shows what the call acts on, else the task or text it passes on.
_LIVE_TOOL_DISPLAY = ToolDisplay(
    primary_candidates=(
        ToolDisplayField("target"),
        ToolDisplayField("agent"),
        ToolDisplayField("program"),
        ToolDisplayField("view"),
        ToolDisplayField("task", quote=True),
        ToolDisplayField("text", quote=True),
    )
)


def register_live_tools(registry: ToolRegistry, hosts: LiveToolHosts) -> None:
    """Register the Live Tools and ``vbot_request``; Agents must opt in to them."""
    for definition in live_tool_definitions():
        registry.register(
            definition["name"],
            definition["description"],
            definition["parameters"],
            _live_tool_handler(hosts),
            summary=_SUMMARIES.get(definition["name"]),
            family=LIVE_TOOL_FAMILY,
            display=_LIVE_TOOL_DISPLAY,
            result_schema={"type": "object"},
            requires_opt_in=True,
            constraints=(TOOL_CONSTRAINT_LIVE_CALL,),
            open_input_schema=True,
            handler_validates_arguments=True,
            coerce_arguments=False,
        )
    request = vbot_request_definition()
    registry.register(
        request["name"],
        request["description"],
        request["parameters"],
        _vbot_request_handler(hosts),
        summary=_SUMMARIES[TOOL_VBOT_REQUEST],
        family=LIVE_TOOL_FAMILY,
        display=ToolDisplay(primary_candidates=(ToolDisplayField("request", quote=True),)),
        result_schema={"type": "object", "required": ["content"]},
        requires_opt_in=True,
        catalog_visible=False,
        constraints=(TOOL_CONSTRAINT_LIVE_CALL,),
        open_input_schema=True,
        handler_validates_arguments=True,
        coerce_arguments=False,
    )


def _live_tool_handler(hosts: LiveToolHosts) -> Callable[..., Any]:
    async def handle(context: ToolContext, arguments: Any) -> JsonObject:
        host = hosts.get(context.session_id or "")
        if host is None:
            return tool_failure("no_live_call", _NO_CALL)
        return await host.run_live_tool(context.tool_name, arguments)

    return handle


def _vbot_request_handler(hosts: LiveToolHosts) -> Callable[..., Any]:
    async def handle(context: ToolContext, arguments: Any) -> JsonObject:
        host = hosts.get(context.session_id or "")
        if host is None:
            return tool_failure("no_live_call", _NO_CALL)
        request = arguments.get("request") if isinstance(arguments, dict) else arguments
        text = request.strip() if isinstance(request, str) else ""
        return await host.vbot_request(text or None)

    return handle


_TARGET_DESCRIPTION = (
    "The Session or Terminal: its ref (such as s2 or t1), or an Agent name when that Agent has "
    "one clear Session."
)


def vbot_request_definition() -> JsonObject:
    """A fresh definition of the voice Agent's request Tool."""
    return {
        "name": TOOL_VBOT_REQUEST,
        "description": (
            "Hand one request to vBot, which operates the app for the user: starting Agents or "
            "coding agents on tasks, sending messages and answers, reading or summarizing "
            "Sessions and Terminals, stopping work, and showing things in the app. The result "
            "arrives later as this Tool's output and ends with what vBot changed for the "
            "request; keep talking with the user meanwhile and do not claim success before it "
            "arrives. Requests run one after another."
        ),
        "parameters": _object(
            {
                "request": _text(
                    "The user's request, passed on faithfully with exact names and wording.",
                    minLength=1,
                    maxLength=MAX_LIVE_TEXT_CHARS,
                )
            },
            required=["request"],
        ),
    }


def live_tool_definitions() -> list[JsonObject]:
    """Fresh definitions of the Live Tools, in ``LIVE_TOOL_NAMES`` order."""
    return [
        {
            "name": TOOL_OVERVIEW,
            "description": (
                "Show what is going on in vBot: what the app shows, the Agents and Projects you "
                "can use, which Sessions are running or recently finished (with the question or "
                "result they ended on), and the coding Terminals. Every Session and Terminal has "
                "a short ref such as s1 or t1; use it to name that Session or Terminal in other "
                "calls."
            ),
            "parameters": _object(
                {
                    "agent": _text(
                        "Show only this Agent's Sessions: its name as listed. Omit it for the "
                        "full overview."
                    )
                }
            ),
        },
        {
            "name": TOOL_START_AGENT_SESSION,
            "description": (
                "Start new Chat Sessions at a vBot Agent, each with the same task. The Sessions "
                "work in the background and appear in the app's sidebar; an update follows when "
                "each one finishes. Use count to send the same Agent several times, for example "
                "to compare results. Codex and Claude Code are not Agents; this Tool cannot "
                "start them."
            ),
            "parameters": _object(
                {
                    "agent": _text("The Agent's name, as overview lists it.", minLength=1),
                    "task": _text(
                        "The task, in the user's words.",
                        minLength=1,
                        maxLength=MAX_LIVE_TEXT_CHARS,
                    ),
                    "count": _count("How many Sessions to start with this task."),
                    "project": _text(
                        "For an Agent of a Project's team that overview does not list: that "
                        "Project's name. Omit it for the Agents overview lists."
                    ),
                },
                required=["agent", "task"],
            ),
        },
        {
            "name": TOOL_START_CODING_TERMINAL,
            "description": (
                "Start the coding agent Codex or Claude Code in new Terminals and type the task "
                "into each once it is ready. The Terminals appear in the app's Terminals view. "
                "Only these two programs run here; a vBot Agent never runs in a Terminal."
            ),
            "parameters": _object(
                {
                    "program": _text(
                        "codex for Codex, claude for Claude Code.", enum=list(LIVE_CODING_PROGRAMS)
                    ),
                    "task": _text(
                        "The task to type in, in the user's words. Omit it to only start the "
                        "program.",
                        maxLength=MAX_LIVE_TEXT_CHARS,
                    ),
                    "folder": _text(
                        "Where to work: a Project name or a folder path. Omit it to use the "
                        "selected Project's folder."
                    ),
                    "count": _count("How many Terminals to start with this task."),
                    "name": _text(
                        "A name for the new Terminals when the user gives one. Omit it otherwise.",
                        maxLength=MAX_LIVE_NAME_CHARS,
                    ),
                },
                required=["program"],
            ),
        },
        {
            "name": TOOL_SEND_MESSAGE,
            "description": (
                "Send a message to an existing Session or coding Terminal, for example the "
                "user's answer to an Agent's question or a follow-up task. A Session that is "
                "still working queues the message. In a Terminal, the text is typed into Codex "
                "or Claude Code running there, never into the command line; to reach an Agent, "
                "send to its Session."
            ),
            "parameters": _object(
                {
                    "target": _text(_TARGET_DESCRIPTION, minLength=1),
                    "text": _text(
                        "The message, in the user's words.",
                        minLength=1,
                        maxLength=MAX_LIVE_TEXT_CHARS,
                    ),
                },
                required=["target", "text"],
            ),
        },
        {
            "name": TOOL_READ_OUTPUT,
            "description": (
                "Read the latest messages of a Session or the screen of a coding Terminal, for "
                "example to summarize a result or find out what an Agent asks."
            ),
            "parameters": _object(
                {"target": _text(_TARGET_DESCRIPTION, minLength=1)}, required=["target"]
            ),
        },
        {
            "name": TOOL_STOP,
            "description": (
                "Stop what a Session or coding Terminal is doing right now. The Session or "
                "Terminal stays open and can get new messages."
            ),
            "parameters": _object(
                {"target": _text(_TARGET_DESCRIPTION, minLength=1)}, required=["target"]
            ),
        },
        {
            "name": TOOL_OPEN,
            "description": (
                "Show something in the vBot app: a Session, a Terminal or Terminal group, an "
                "Agent's page, a Project's page, or one of the app's views."
            ),
            "parameters": _object(
                {
                    "target": _text(
                        "What to show: a Session or Terminal ref, a Terminal group name, an "
                        "Agent name (opens its latest Session in the chat), or a Project name. "
                        "Omit it to open a view."
                    ),
                    "view": _text(
                        "Without a target, the view to open. With a target, the kind of thing "
                        "it names: chat (a Session, or an Agent's latest Session: use this when "
                        "the user wants to see an Agent or its chat), terminals (a Terminal or "
                        "group), agents (an Agent's settings page, only when the user asks for "
                        "that page or its settings), projects (a Project's page).",
                        enum=list(LIVE_VIEWS),
                    ),
                }
            ),
        },
        {
            "name": TOOL_MANAGE_TERMINALS,
            "description": (
                "Arrange or close coding Terminals. maximize shows one Terminal alone and "
                "restore returns to the group layout. key presses one key in a Terminal, for "
                "example to answer a menu. close stops a Terminal and removes it; use it only "
                "when the user asks to close that Terminal. reorder sets the order of the "
                "Terminals in a group. create_group, rename_group and delete_group manage "
                "Terminal groups; delete_group also stops every Terminal in the group."
            ),
            "parameters": _object(
                {
                    "action": _text("What to do.", enum=list(LIVE_TERMINAL_ACTIONS)),
                    "target": _text(
                        "For maximize, key and close: the Terminal ref (such as t1). For "
                        "rename_group and delete_group: the group name. For reorder: the group "
                        "name, needed only when the Terminals are in different groups. Omit it "
                        "for restore and create_group."
                    ),
                    "key": _text(
                        "Required for key: the key to press.",
                        enum=list(LIVE_KEYS),
                    ),
                    "name": _text(
                        "The new group name for create_group or rename_group.",
                        maxLength=MAX_LIVE_NAME_CHARS,
                    ),
                    "order": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For reorder: every Terminal ref of the group, in the new order."
                        ),
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "For close and delete_group: true only after the user agreed to "
                            "close Terminals that are still working. Omit it otherwise."
                        ),
                    },
                },
                required=["action"],
            ),
        },
        {
            "name": TOOL_END_CALL,
            "description": (
                "End this voice call when the user says goodbye, asks to hang up, or tells you "
                "to go to sleep or be quiet. It closes and stops nothing; running work goes on."
            ),
            "parameters": _object({}),
        },
    ]


def _object(properties: JsonObject, *, required: list[str] | None = None) -> JsonObject:
    schema: JsonObject = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _text(description: str, **schema: Any) -> JsonObject:
    return {"type": "string", "description": description, **schema}


def _count(description: str) -> JsonObject:
    return {
        "type": "integer",
        "description": description,
        "minimum": 1,
        "maximum": MAX_LIVE_COUNT,
        "default": 1,
    }
