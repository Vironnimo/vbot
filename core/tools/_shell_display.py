"""Command details shared by shell calls and background task status and stop calls."""

from core.tools._shell_arguments import shell_display_values
from core.tools._shell_hint_pipeline import reported_cmdlet_error_hint
from core.tools._shell_results import duration, exit_code_text, stopped_because
from core.tools._tool_display import display_notice, display_text
from core.tools.command_manager import CommandReport
from core.tools.tools import JsonObject, ToolContext


def shell_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    command, _ = shell_display_values(arguments)
    if command:
        blocks.append(display_text("command", text=command))
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if isinstance(data, dict):
        blocks.extend(command_result_details(data))
    return blocks


def record_command_stop(
    context: ToolContext,
    report: CommandReport,
    *,
    subagent: str | None = None,
    stopped_here: bool = False,
) -> None:
    """Name who stopped the command from the perspective of the user viewing this Session."""
    reason = report.stop_reason
    if reason is None or report.not_run is not None:
        return
    if reason == "user":
        message = f"You stopped the command after {duration(report.elapsed_seconds)}."
    elif stopped_here or reason == "agent":
        actor = (
            f"Sub-Agent {subagent}"
            if subagent and not stopped_here
            else "The Agent in this Session"
        )
        message = f"{actor} stopped the command."
    elif reason == "parent":
        owner = f"Sub-Agent {subagent}" if subagent else "the Agent in this Session"
        message = f"An Agent above {owner} stopped the command."
    else:
        message = f"Stopped: {stopped_because(report)}"
    context.add_display_notice("warning", message)


def command_result_details(data: JsonObject) -> list[JsonObject]:
    """Show the output and each fact about what ran, including processes still alive."""
    blocks = [display_text("output", source="result", path=("data", "output"))]
    status = data.get("status")
    if status == "running":
        blocks.append(display_notice("info", "The command continues in the background."))
    if status == "not_run":
        reason = data.get("not_run_because")
        message = f"The command did not run: {reason}" if reason else "The command did not run."
        blocks.append(display_notice("warning", message))
    code = data.get("exit_code")
    if status == "exited" and isinstance(code, int) and not isinstance(code, bool) and code:
        blocks.append(
            display_notice("warning", f"The command exited with code {exit_code_text(code)}.")
        )
    elif status == "exited" and code is None:
        blocks.append(display_notice("warning", "The command exited with an unknown exit code."))
    failed = data.get("failed_programs")
    if isinstance(failed, list) and failed:
        blocks.append(display_notice("warning", "; ".join(str(item) for item in failed)))
    hint = data.get("hint")
    if isinstance(hint, str) and hint.strip():
        partial = status == "exited" and code == 0 and reported_cmdlet_error_hint(hint)
        blocks.append(display_notice("error" if partial else "info", f"Hint: {hint.strip()}"))
    processes = data.get("still_running")
    if isinstance(processes, list) and processes:
        names = ", ".join(str(item) for item in processes)
        blocks.append(display_notice("info", f"Processes it started still run: {names}."))
    notes = data.get("notes")
    if isinstance(notes, list):
        blocks.extend(
            display_notice("info", note) for note in notes if isinstance(note, str) and note.strip()
        )
    return blocks
