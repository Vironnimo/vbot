"""Tool registry, definitions, result envelopes, and execution scheduling."""

from core.tools.bash import (
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_NAME,
    BASH_TOOL_PARAMETERS,
    bash_handler,
    register_bash_tool,
)
from core.tools.edit import (
    EDIT_TOOL_DESCRIPTION,
    EDIT_TOOL_NAME,
    EDIT_TOOL_PARAMETERS,
    edit_handler,
    register_edit_tool,
)
from core.tools.glob import (
    GLOB_TOOL_DESCRIPTION,
    GLOB_TOOL_NAME,
    GLOB_TOOL_PARAMETERS,
    glob_handler,
    register_glob_tool,
)
from core.tools.grep import (
    GREP_TOOL_DESCRIPTION,
    GREP_TOOL_NAME,
    GREP_TOOL_PARAMETERS,
    grep_handler,
    register_grep_tool,
)
from core.tools.process import (
    PROCESS_TOOL_DESCRIPTION,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_PARAMETERS,
    make_process_handler,
    register_process_tool,
)
from core.tools.read import (
    READ_TOOL_DESCRIPTION,
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
    read_handler,
    register_read_tool,
)
from core.tools.session_search import (
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
    make_session_search_handler,
    register_session_search_tool,
    session_search_handler,
)
from core.tools.skill import (
    SKILL_TOOL_DESCRIPTION,
    SKILL_TOOL_NAME,
    SKILL_TOOL_PARAMETERS,
    make_skill_handler,
    register_skill_tool,
)
from core.tools.tools import (
    DEFAULT_TOOL_CONCURRENCY_LIMIT,
    TOOL_ALLOWLIST_WILDCARD,
    DuplicateToolError,
    InvalidToolResultError,
    JsonObject,
    Tool,
    ToolCall,
    ToolCancellationHook,
    ToolContext,
    ToolDisplay,
    ToolEmitHook,
    ToolError,
    ToolExecutionConfig,
    ToolExecutor,
    ToolHandler,
    ToolNotAllowedError,
    ToolNoteHook,
    ToolNotFoundError,
    ToolRegistry,
    ToolSkillActivationHook,
    is_tool_result_envelope,
    tool_failure,
    tool_success,
)
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
    register_web_fetch_tool,
    web_fetch_handler,
)
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
    register_web_search_tool,
    web_search_handler,
)
from core.tools.write import (
    WRITE_TOOL_DESCRIPTION,
    WRITE_TOOL_NAME,
    WRITE_TOOL_PARAMETERS,
    register_write_tool,
    write_handler,
)


def __getattr__(name: str) -> object:
    if name == "register_cron_tool":
        from core.tools.cron import register_cron_tool

        return register_cron_tool
    if name == "register_subagent_tools":
        from core.tools.subagent import register_subagent_tools

        return register_subagent_tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BASH_TOOL_DESCRIPTION",
    "BASH_TOOL_NAME",
    "BASH_TOOL_PARAMETERS",
    "DEFAULT_TOOL_CONCURRENCY_LIMIT",
    "DuplicateToolError",
    "EDIT_TOOL_DESCRIPTION",
    "EDIT_TOOL_NAME",
    "EDIT_TOOL_PARAMETERS",
    "GLOB_TOOL_DESCRIPTION",
    "GLOB_TOOL_NAME",
    "GLOB_TOOL_PARAMETERS",
    "GREP_TOOL_DESCRIPTION",
    "GREP_TOOL_NAME",
    "GREP_TOOL_PARAMETERS",
    "InvalidToolResultError",
    "JsonObject",
    "PROCESS_TOOL_DESCRIPTION",
    "PROCESS_TOOL_NAME",
    "PROCESS_TOOL_PARAMETERS",
    "READ_TOOL_DESCRIPTION",
    "READ_TOOL_NAME",
    "READ_TOOL_PARAMETERS",
    "SESSION_SEARCH_TOOL_DESCRIPTION",
    "SESSION_SEARCH_TOOL_NAME",
    "SESSION_SEARCH_TOOL_PARAMETERS",
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_NAME",
    "SKILL_TOOL_PARAMETERS",
    "TOOL_ALLOWLIST_WILDCARD",
    "Tool",
    "ToolCall",
    "ToolCancellationHook",
    "ToolContext",
    "ToolDisplay",
    "ToolEmitHook",
    "ToolError",
    "ToolExecutionConfig",
    "ToolExecutor",
    "ToolHandler",
    "ToolNoteHook",
    "ToolNotAllowedError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolSkillActivationHook",
    "WEB_FETCH_TOOL_DESCRIPTION",
    "WEB_FETCH_TOOL_NAME",
    "WEB_FETCH_TOOL_PARAMETERS",
    "WEB_SEARCH_TOOL_DESCRIPTION",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_PARAMETERS",
    "WRITE_TOOL_DESCRIPTION",
    "WRITE_TOOL_NAME",
    "WRITE_TOOL_PARAMETERS",
    "bash_handler",
    "edit_handler",
    "glob_handler",
    "grep_handler",
    "is_tool_result_envelope",
    "make_process_handler",
    "make_skill_handler",
    "read_handler",
    "make_session_search_handler",
    "register_edit_tool",
    "register_glob_tool",
    "register_bash_tool",
    "register_cron_tool",
    "register_grep_tool",
    "register_process_tool",
    "register_read_tool",
    "register_session_search_tool",
    "register_skill_tool",
    "register_subagent_tools",
    "register_web_fetch_tool",
    "register_web_search_tool",
    "register_write_tool",
    "session_search_handler",
    "tool_failure",
    "tool_success",
    "web_fetch_handler",
    "web_search_handler",
    "write_handler",
]
