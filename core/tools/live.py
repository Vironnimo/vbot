"""The Live Tools: how the Agents of a Live voice call operate vBot.

A Live call has two built-in Agents (``core.agents``): the voice Agent, whose
Model talks with the user, and the backend Agent, which answers the requests
the voice Model hands on. Both reach the app through these Tools. They only
organize work: look at what runs, start and steer Agents and coding agents, and
show things in the app.
"""

from __future__ import annotations

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
# The voice Agent's line to the backend Agent; offered only while a call uses it.
TOOL_VBOT_REQUEST = "vbot_request"
LIVE_TOOL_FAMILY = "live"
# Marks the Tools only the Agents of a Live call can use.
TOOL_CONSTRAINT_LIVE_CALL = "live_call"
