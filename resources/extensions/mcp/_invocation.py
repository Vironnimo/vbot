"""Canonical Session identity across the MCP connection Tool's delegated dispatch."""

from contextvars import ContextVar

from core.tools.tools import ToolContext

CANONICAL_CONTEXT: ContextVar[ToolContext | None] = ContextVar(
    "mcp_canonical_context", default=None
)
RETAIN_CALL: ContextVar[bool] = ContextVar("mcp_retain_call", default=False)
