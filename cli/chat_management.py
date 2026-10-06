"""``vbot chat``: send one message to an Agent Session and follow its Run to the end.

The command talks to the server only through RPC (``session.*``, ``chat.stream``,
``chat.cancel``) and the Run's SSE stream. A message for a new Session is sent
with ``new_session``, so the server creates the Session only for a message it
accepts. It writes the answer to stdout itself,
because streamed text cannot wait for the end of the Run; the outcome line and
recovery guidance stay with the shared CLI output owner.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

from cli._parser_agents import DEFAULT_CHAT_AGENT
from cli._progress import Status, status_line
from cli._recovery import format_command
from cli.formatting import output_mode
from cli.rpc_client import RunEventStreamError, stream_run_events
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_STARTED_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    TERMINAL_EVENT_TYPES,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
)

JsonObject = dict[str, Any]

# Token fields a Model step reports; the Run's Usage is their sum over its steps.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_PREVIEW_CHARACTERS = 500
_PROGRESS_CHARACTERS = 200
# The caller waits for streamed events in short polls so Ctrl-C lands promptly.
_POLL_SECONDS = 0.2
# Reconnects in a row that deliver no new event before the stream counts as lost.
_RECONNECT_LIMIT = 3


@dataclass(frozen=True)
class ChatRequest:
    """One ``vbot chat`` invocation after argument parsing and stdin reading.

    A new Session works in ``working_project_id``, or with ``workspace`` in the
    Agent's Workspace; with neither, in the Agent's default Project.
    """

    agent: str
    prompt: str
    session_id: str | None = None
    continue_latest: bool = False
    overrides: JsonObject = field(default_factory=dict)
    working_project_id: str | None = None
    workspace: bool = False
    json_output: bool = False


def chat(instance: ServerInstance, request: ChatRequest) -> CommandResult:
    """Select the Session (or a new one), send the message and report the Run's outcome.

    The answer (or JSON report) is written to stdout here; the returned result
    carries the outcome sentence for stderr and any RPC failure evidence.
    """
    selected = _select_session(instance, request)
    if isinstance(selected, CommandResult):
        return selected
    session_id, stored_overrides = selected

    params: JsonObject = {"agent_id": request.agent, "content": request.prompt}
    if session_id is None:
        # The new Session starts with the overrides and its working Project, and
        # exists only once accepted.
        new_session: JsonObject = (
            {"agent_overrides": dict(request.overrides)} if request.overrides else {}
        )
        if request.workspace:
            new_session["working_project_id"] = None
        elif request.working_project_id is not None:
            new_session["working_project_id"] = request.working_project_id
        params["new_session"] = new_session
    else:
        params["session_id"] = session_id
    payload = _rpc_call(instance, "chat.stream", params)
    if not payload.ok:
        return payload.to_command_result()
    data = payload.data
    if session_id is None:
        # A command can run without creating the Session; then none is named.
        session_id = _text(data.get("session_id"))
        if session_id is not None and request.overrides:
            stored_overrides = dict(request.overrides)
    report = _Report(request.agent, session_id, stored_overrides)
    if data.get("queued") is True:
        return _queued(instance, request, report, data.get("item"))
    if data.get("command_handled") is True:
        return _command_handled(instance, request, report, data)

    run_id = data.get("run_id")
    sse_path = data.get("sse_url")
    if not isinstance(run_id, str) or not isinstance(sse_path, str) or report.session_id is None:
        return CommandResult(
            ok=False,
            message="chat.stream result names no Run, queued item or command reply",
            instance=instance,
        )
    mode = output_mode.get()
    timeline = _RunTimeline(
        stream_text=not request.json_output and mode != "plain" and _is_terminal(sys.stdout),
        show_progress=mode == "human" or (mode == "auto" and _is_terminal(sys.stderr)),
    )
    try:
        for event in data.get("events") or ():
            if isinstance(event, dict) and timeline.apply(event):
                break
        lost = None if timeline.finished else _follow(instance, sse_path, timeline)
    except KeyboardInterrupt:
        _cancel_after_interrupt(instance, run_id, timeline)
        raise
    return _run_outcome(instance, request, report, report.session_id, run_id, timeline, lost)


def _select_session(
    instance: ServerInstance, request: ChatRequest
) -> tuple[str | None, JsonObject | None] | CommandResult:
    """Return the target Session id (``None`` for a new one) and the overrides saved here.

    Without ``--session`` or ``-c`` the message starts a new Session. ``-c``
    continues the latest conversation, or starts a new one when the Agent has
    none yet.
    """
    session_id = request.session_id
    if session_id is None:
        if not request.continue_latest:
            return None, None
        latest = _latest_session(instance, request.agent)
        if isinstance(latest, CommandResult):
            return latest
        if latest is None:
            return None, None
        session_id = latest
    if not request.overrides:
        return session_id, None
    payload = _rpc_call(
        instance,
        "session.set_agent_overrides",
        {
            "agent_id": request.agent,
            "session_id": session_id,
            "agent_overrides": dict(request.overrides),
        },
    )
    if not payload.ok:
        return payload.to_command_result()
    return session_id, _object_or_none(payload.data.get("agent_overrides"))


def _latest_session(instance: ServerInstance, agent: str) -> str | None | CommandResult:
    """The Agent's most recently active conversation, skipping automated Sessions."""
    payload = _rpc_call(
        instance,
        "session.list",
        {
            "agent_id": agent,
            "limit": 1,
            "include_subagents": False,
            "include_memory_reflections": False,
            "include_skill_reflections": False,
            "include_cron": False,
            "include_channels": False,
        },
    )
    if not payload.ok:
        return payload.to_command_result()
    sessions = payload.data.get("sessions")
    if not isinstance(sessions, list):
        return CommandResult(
            ok=False, message="RPC result missing sessions list", instance=instance
        )
    if not sessions:
        return None
    first = sessions[0]
    session_id = first.get("id") if isinstance(first, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return CommandResult(
            ok=False, message="session.list returned a Session without id", instance=instance
        )
    return session_id


@dataclass
class _Report:
    """Facts every outcome shares; rendered as the ``--json`` object."""

    agent: str
    # ``None`` when a command ran without creating the new Session.
    session_id: str | None
    agent_overrides: JsonObject | None

    def json_object(self, **fields: Any) -> JsonObject:
        report: JsonObject = {
            "agent_id": self.agent,
            "session_id": self.session_id,
            "run_id": None,
            "status": None,
            "model": None,
            "message": "",
            "tool_calls": [],
            "usage": None,
            "duration_ms": None,
            "error": None,
            "agent_overrides": self.agent_overrides,
            "queue_item": None,
            "command": None,
        }
        report.update(fields)
        return report


def _print_json(report: JsonObject) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def _queued(
    instance: ServerInstance, request: ChatRequest, report: _Report, item: Any
) -> CommandResult:
    item = item if isinstance(item, dict) else {}
    item_id = item.get("id") if isinstance(item.get("id"), str) else "?"
    if request.json_output:
        _print_json(report.json_object(status="queued", queue_item=item))
    return CommandResult(
        ok=False,
        message=(
            f"Session {report.session_id} is busy with another Run. The message is queued "
            f"as {item_id} and starts when that Run ends; vbot chat does not wait for it. "
            "Sending it again queues a second copy."
        ),
        instance=instance,
    )


def _command_handled(
    instance: ServerInstance, request: ChatRequest, report: _Report, data: Mapping[str, Any]
) -> CommandResult:
    reply = _text(data.get("reply")) or ""
    facts = _object_or_none(data.get("data")) or {}
    if request.json_output:
        _print_json(report.json_object(status="command", message=reply, command=facts))
    elif reply:
        print(reply, flush=True)
    command = _text(facts.get("command")) or "command"
    # A command can move the conversation, for example to another Session.
    agent = _text(facts.get("agent_id")) or request.agent
    session_id = _text(facts.get("session_id")) or report.session_id
    if report.session_id is None:
        lines = [f"{command} handled without a Session (Agent {request.agent})"]
    else:
        lines = [f"{command} handled in Session {report.session_id} (Agent {request.agent})"]
    if session_id is not None:
        lines.append(_continue_line(agent, session_id))
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _run_outcome(
    instance: ServerInstance,
    request: ChatRequest,
    report: _Report,
    session_id: str,
    run_id: str,
    timeline: _RunTimeline,
    lost: str | None,
) -> CommandResult:
    terminal = timeline.terminal or {}
    status = timeline.status if lost is None else "unknown"
    error = _run_error(timeline) if status in {"failed", "interrupted"} else lost
    if request.json_output:
        _print_json(
            report.json_object(
                run_id=run_id,
                status=status,
                model=timeline.model,
                message=timeline.message,
                tool_calls=list(timeline.tool_calls.values()),
                usage=timeline.usage or None,
                duration_ms=_duration_ms(terminal),
                error=error,
            )
        )
    elif not timeline.stream_text and timeline.message:
        print(timeline.message, flush=True)

    model = timeline.model or "unknown"
    where = f"in Session {session_id} (Agent {request.agent}, Model {model})"
    if lost is not None:
        status_command = _chat_command(request.agent, session_id, "/status")
        return CommandResult(
            ok=False,
            message=(
                f"lost the event stream of Run {run_id} {where}: {lost}. The Run can still be "
                f"running; check it with {status_command} before sending the message again."
            ),
            instance=instance,
        )
    if status == "completed":
        lines = [f"completed {where}"]
        if report.agent_overrides:
            saved = ", ".join(f"{key}={value}" for key, value in report.agent_overrides.items())
            lines.append(f"Session overrides: {saved} (kept for later messages in this Session)")
        lines.append(_continue_line(request.agent, session_id))
        return CommandResult(ok=True, message="\n".join(lines), instance=instance)
    if status == "cancelled":
        detail = f"Run {run_id} was cancelled {where}"
    elif status == "interrupted":
        cause = terminal.get("cause")
        detail = f"Run {run_id} was interrupted ({cause}) {where}: {error}"
    else:
        detail = f"Run {run_id} ended {status} {where}: {error}"
    return CommandResult(ok=False, message=detail, instance=instance)


def _run_error(timeline: _RunTimeline) -> str:
    terminal = timeline.terminal or {}
    error = terminal.get("error")
    if isinstance(error, str) and error:
        return error
    return timeline.error_text or "no error details were reported"


def _duration_ms(terminal: JsonObject) -> int | None:
    timing = terminal.get("timing")
    duration = timing.get("duration_ms") if isinstance(timing, dict) else None
    return duration if isinstance(duration, int) and not isinstance(duration, bool) else None


def _continue_line(agent: str, session_id: str) -> str:
    return f"Continue: {_chat_command(agent, session_id)} <message>"


def _chat_command(agent: str, session_id: str, *tail: str) -> str:
    agent_option = () if agent == DEFAULT_CHAT_AGENT else ("--agent", agent)
    return format_command(("vbot", "chat", "--session", session_id, *agent_option, *tail))


def _cancel_after_interrupt(instance: ServerInstance, run_id: str, timeline: _RunTimeline) -> None:
    """Ctrl-C stops the Run too, not only the local reporting."""
    timeline.end_text_line()
    payload = _rpc_call(instance, "chat.cancel", {"run_id": run_id, "reason": "user"})
    if payload.ok:
        note = f"chat: requested cancellation of Run {run_id}"
    else:
        reason = payload.message.splitlines()[0] if payload.message else "no details"
        note = f"chat: could not cancel Run {run_id}: {reason}"
    print(status_line("warning", note, stream=sys.stderr), file=sys.stderr, flush=True)


def _follow(instance: ServerInstance, sse_path: str, timeline: _RunTimeline) -> str | None:
    """Apply streamed events until the terminal one; return why the stream was lost."""
    stalled = 0
    while True:
        before = timeline.last_sequence
        events = stream_run_events(instance, sse_path, after_sequence=before)
        try:
            for event in _in_background(events):
                if timeline.apply(event):
                    return None
            problem = "the stream closed before the Run finished"
        except RunEventStreamError as error:
            if error.status_code == 404:
                return "the server no longer holds this Run"
            problem = str(error)
        stalled = 0 if timeline.last_sequence > before else stalled + 1
        if stalled >= _RECONNECT_LIMIT:
            return problem


def _in_background(events: Iterator[JsonObject]) -> Iterator[JsonObject]:
    """Iterate ``events`` on a worker thread while this thread waits in short polls.

    A blocking socket read does not see Ctrl-C on Windows until data arrives;
    the polling thread does. What the worker raises is raised here.
    """
    handoff: queue.Queue[tuple[str, Any]] = queue.Queue()
    stop = threading.Event()

    def pump() -> None:
        try:
            for item in events:
                if stop.is_set():
                    break
                handoff.put(("item", item))
            else:
                handoff.put(("end", None))
        except BaseException as error:  # noqa: BLE001 - re-raised by the consumer
            handoff.put(("error", error))
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()

    threading.Thread(target=pump, name="vbot-chat-events", daemon=True).start()
    try:
        while True:
            try:
                kind, value = handoff.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                continue
            if kind == "item":
                yield value
            elif kind == "end":
                return
            else:
                raise value
    finally:
        stop.set()


class _RunTimeline:
    """Fold one Run's events into its outcome, printing text and progress as they arrive."""

    def __init__(self, *, stream_text: bool, show_progress: bool) -> None:
        self.stream_text = stream_text
        self.show_progress = show_progress
        self.last_sequence = 0
        self.terminal: JsonObject | None = None
        self.status: str | None = None
        self.message = ""
        self.tool_calls: dict[str, JsonObject] = {}
        self.usage: dict[str, int] = {}
        self.error_text: str | None = None
        self._answer_model: str | None = None
        self._request_model: str | None = None
        # Whether the current Model step's text reached stdout through deltas.
        self._step_streamed = False
        self._line_open = False

    @property
    def finished(self) -> bool:
        return self.terminal is not None

    @property
    def model(self) -> str | None:
        return self._answer_model or self._request_model

    def apply(self, event: JsonObject) -> bool:
        """Apply one event once; return whether the Run has ended."""
        sequence = event.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            if sequence <= self.last_sequence:
                return self.finished
            self.last_sequence = sequence
        kind = event.get("type")
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == ASSISTANT_OUTPUT_DELTA_EVENT:
            text = payload.get("content_delta")
            if isinstance(text, str) and text:
                self._step_streamed = True
                if self.stream_text:
                    self._write(text)
        elif kind == ASSISTANT_OUTPUT_EVENT:
            self._assistant_output(payload.get("message"))
        elif kind == STREAM_ATTEMPT_RESTARTED_EVENT:
            if self._step_streamed and self.stream_text:
                self._progress(
                    "warning", "The Model restarted its response; the partial text above is void"
                )
            self._step_streamed = False
        elif kind == TOOL_CALL_STARTED_EVENT:
            self._tool_call_started(payload)
        elif kind == TOOL_CALL_RESULT_EVENT:
            self._tool_call_result(payload)
        elif kind == PROVIDER_REQUEST_STATUS_EVENT:
            self._request_status(payload)
        elif kind == MODEL_FALLBACK_ACTIVATED_EVENT:
            to_model = payload.get("to_model")
            if isinstance(to_model, str) and to_model:
                self._request_model = to_model
            self._progress(
                "warning",
                f"Model {payload.get('from_model')} unavailable; continuing with {to_model}",
            )
        elif kind == MODEL_STEP_USAGE_EVENT:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                for name in _USAGE_FIELDS:
                    value = usage.get(name)
                    if isinstance(value, int) and not isinstance(value, bool):
                        self.usage[name] = self.usage.get(name, 0) + value
        elif kind == ERROR_MESSAGE_PERSISTED_EVENT:
            message = payload.get("message")
            text = _message_text(message.get("content")) if isinstance(message, dict) else ""
            self.error_text = text or self.error_text
        elif kind == COMPACTION_STARTED_EVENT:
            self._progress("info", "Compacting the Session context")
        elif kind in TERMINAL_EVENT_TYPES:
            self.terminal = payload
            status = payload.get("status")
            self.status = status if isinstance(status, str) else str(kind).removeprefix("run_")
            self.end_text_line()
        return self.finished

    def end_text_line(self) -> None:
        """Finish a partly written line of streamed text."""
        if self._line_open:
            self._write("\n")

    def _assistant_output(self, message: Any) -> None:
        if isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and model:
                self._answer_model = model
            text = _message_text(message.get("content"))
            if text:
                self.message = text
                if self.stream_text and not self._step_streamed:
                    self._write(text)
        self._step_streamed = False
        self.end_text_line()

    def _tool_call_started(self, payload: JsonObject) -> None:
        call = payload.get("tool_call")
        call = call if isinstance(call, dict) else {}
        entry = self._tool_entry(call)
        arguments = call.get("arguments")
        if isinstance(arguments, dict):
            entry["arguments"] = arguments
        summary = _argument_summary(entry["arguments"], payload.get("display"))
        name = entry["name"]
        self._progress("info", f"Tool {name}: {summary}" if summary else f"Tool {name}")

    def _tool_call_result(self, payload: JsonObject) -> None:
        call = payload.get("tool_call")
        entry = self._tool_entry(call if isinstance(call, dict) else {})
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        if result.get("ok") is True:
            entry["is_error"] = False
            entry["error"] = None
            entry["result_preview"] = _preview(result.get("data"))
            return
        error = result.get("error")
        if not isinstance(error, dict):
            error = {"code": payload.get("error_code"), "message": "no error details"}
        entry["is_error"] = True
        entry["error"] = error
        self._progress(
            "warning", f"Tool {entry['name']} failed: {error.get('code')}: {error.get('message')}"
        )

    def _tool_entry(self, call: JsonObject) -> JsonObject:
        call_id = call.get("id")
        key = call_id if isinstance(call_id, str) and call_id else f"#{len(self.tool_calls)}"
        name = call.get("name")
        return self.tool_calls.setdefault(
            key,
            {
                "id": call_id if isinstance(call_id, str) else None,
                "name": name if isinstance(name, str) else "?",
                "arguments": {},
                "is_error": False,
                "error": None,
                "result_preview": None,
            },
        )

    def _request_status(self, payload: JsonObject) -> None:
        model = payload.get("model")
        if isinstance(model, str) and model:
            self._request_model = model
        if payload.get("state") != "retrying":
            return
        details = []
        error_kind = payload.get("error_kind")
        if isinstance(error_kind, str) and error_kind:
            details.append(error_kind)
        attempt, limit = payload.get("attempt"), payload.get("max_attempts")
        if isinstance(attempt, int) and isinstance(limit, int):
            details.append(f"attempt {attempt}/{limit}")
        delay = payload.get("delay_seconds")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool):
            details.append(f"in {delay:g}s")
        suffix = f" ({', '.join(details)})" if details else ""
        self._progress("warning", f"Model request of {self.model or 'the Model'} retrying{suffix}")

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self._line_open = not text.endswith("\n")

    def _progress(self, status: Status, message: str) -> None:
        if not self.show_progress:
            return
        self.end_text_line()
        print(
            status_line(status, _one_line(message, _PROGRESS_CHARACTERS), stream=sys.stderr),
            file=sys.stderr,
            flush=True,
        )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _argument_summary(arguments: JsonObject, display: Any) -> str:
    """The Tool's own display summary, else its visible arguments as compact JSON."""
    display = display if isinstance(display, dict) else {}
    summary = display.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    hidden = display.get("hidden_argument_keys")
    hidden_keys = set(hidden) if isinstance(hidden, list) else set()
    visible = {key: value for key, value in arguments.items() if key not in hidden_keys}
    return (_preview(visible) or "") if visible else ""


def _preview(value: Any) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return _one_line(text, _PREVIEW_CHARACTERS)


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _object_or_none(value: Any) -> JsonObject | None:
    return dict(value) if isinstance(value, dict) and value else None


def _is_terminal(stream: TextIO | None) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())
