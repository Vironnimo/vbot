"""Command details shared by shell calls and background task status and stop calls."""

from core.tools._shell_arguments import shell_display_values
from core.tools._shell_results import exit_code_text
from core.tools._tool_display import display_notice, display_text
from core.tools.tools import JsonObject


def shell_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    command, _ = shell_display_values(arguments)
    if command:
        blocks.append(display_text("command", text=command))
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if isinstance(data, dict):
        blocks.extend(command_result_details(data))
    return blocks


def command_result_details(data: JsonObject) -> list[JsonObject]:
    """Show the output and each fact about what ran, including processes still alive."""
    blocks = [display_text("output", source="result", path=("data", "output"))]
    status = data.get("status")
    if status == "running":
        blocks.append(display_notice("info", "The command continues in the background."))
    if status == "stopped" and isinstance(data.get("stopped_because"), str):
        blocks.append(display_notice("warning", f"Stopped: {data['stopped_because']}"))
    if status == "not_run":
        reason = data.get("not_run_because")
        message = f"The command did not run: {reason}" if reason else "The command did not run."
        blocks.append(display_notice("warning", message))
    code = data.get("exit_code")
    if status == "exited" and isinstance(code, int) and not isinstance(code, bool) and code:
        blocks.append(
            display_notice("warning", f"The command exited with code {exit_code_text(code)}.")
        )
    failed = data.get("failed_programs")
    if isinstance(failed, list) and failed:
        blocks.append(display_notice("warning", "; ".join(str(item) for item in failed)))
    if isinstance(data.get("hint"), str):
        blocks.append(display_notice("info", f"Hint: {data['hint']}"))
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
