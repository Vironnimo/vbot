"""Drive the Swarm scenario (``--scenario swarm``) through the public RPC edge.

Per level the harness saves one profile with N participants on the fake
Provider's Model, starts one Swarm of the bundled Swarm Extension whose goal
carries the ``[[perf ...]]`` directive, and watches it until every participant
has completed its turn budget (or, with a duration, until the wind-down let the
in-flight turns finish) and the Swarm is idle. Management goes through
``extensions.operation`` and history through ``extensions.page_history``, the
same calls the Swarm page makes.

A participant takes a new turn only when a Board post wakes it. A peer's post
that arrives while a participant is still running is delivered inside that Run
and wakes nobody, so a Swarm can fall idle with turns left. The harness then
posts a short user message (a *kick*): user posts wake every participant, also
one in a quiet period. Kicks are counted and reported; the fake Provider scripts
the rest (see :mod:`scripts.perf_load_suite.swarm_script`).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.perf_load_suite.fake_provider import MODEL_ID
from scripts.perf_load_suite.fixture import PROJECT_DISPLAY_NAME
from scripts.perf_load_suite.rpc import RpcCaller
from scripts.perf_load_suite.stack import FAKE_PROVIDER_ID, FakeProvider
from scripts.perf_load_suite.swarm_script import (
    is_swarm_tool,
    participant_for_call_id,
    split_tool_spec,
)

SWARM_EXTENSION = "swarm"
SWARM_PAGE_ID = "swarms"
SWARM_VIEW = f"extension:{SWARM_EXTENSION}:{SWARM_PAGE_ID}"
CATALOG_MODEL = f"{FAKE_PROVIDER_ID}/{MODEL_ID}"
POLL_SECONDS = 1.0
FINISH_IDLE_POLLS = 2
KICK_IDLE_POLLS = 3
MAX_KICKS_WITHOUT_PROGRESS = 5
HISTORY_PAGE_LIMIT = 100
MAX_HISTORY_PAGES = 10_000
GOAL_TEXT = "Work through the scripted performance turns together on the Board."
KICK_TEXT = "Harness check-in {number}: continue with the remaining scripted work."
_ATTENTION_STATES = frozenset({"needs_attention", "stopping", "cancelled", "interrupted", "failed"})


class SwarmError(RuntimeError):
    """The Swarm could not be set up, run to completion, or read back."""


def swarm_operation(rpc: RpcCaller, operation: str, arguments: dict[str, Any]) -> Any:
    """Invoke one Swarm management operation."""
    return rpc.call(
        "extensions.operation",
        {"name": SWARM_EXTENSION, "operation": operation, "arguments": arguments},
    )


@dataclass(frozen=True)
class SwarmSetup:
    """A saved profile and the Swarm page registration for one level."""

    profile_id: str
    page: dict[str, str]
    allowed_tools: tuple[str, ...]


def prepare_swarm(
    rpc: RpcCaller, *, fixture_dir: Path, participants: int, tools: Sequence[str]
) -> SwarmSetup:
    """Check the bundled Swarm Extension and save a profile of ``participants``.

    The profile works in the fixture Project and may use every catalog Tool
    within the Project's Tool ceiling that needs no opt-in; the directive's
    regular Tools must be among them.
    """
    extensions = rpc.call("extensions.list", {}).get("extensions") or []
    record = next((item for item in extensions if item.get("name") == SWARM_EXTENSION), None)
    if record is None or record.get("status") != "loaded":
        state = "missing" if record is None else f"{record.get('status')}: {record.get('error')}"
        raise SwarmError(f"the bundled Swarm Extension is not loaded ({state})")
    pages = rpc.call("extensions.pages", {}).get("pages") or []
    page = next(
        (
            item
            for item in pages
            if item.get("extension") == SWARM_EXTENSION and item.get("page") == SWARM_PAGE_ID
        ),
        None,
    )
    if page is None:
        raise SwarmError("the Swarm Extension registered no swarms page")
    project = rpc.call(
        "project.add", {"cwd": str(fixture_dir), "display_name": PROJECT_DISPLAY_NAME}
    )["project"]
    catalog = swarm_operation(rpc, "catalog", {})["catalog"]
    models = {item.get("id") for item in catalog.get("models") or []}
    if CATALOG_MODEL not in models:
        raise SwarmError(f"the Swarm catalog lacks the fake Provider Model {CATALOG_MODEL}")
    project_id = str(project["project_id"])
    ceiling = next(
        (
            set(item.get("allowed_tools") or [])
            for item in catalog.get("projects") or []
            if item.get("id") == project_id
        ),
        None,
    )
    if ceiling is None:
        raise SwarmError(f"the Swarm catalog lacks the fixture Project {project_id}")
    allowed = tuple(
        str(item["name"])
        for item in catalog.get("tools") or []
        if item.get("name") in ceiling
        and not item.get("requires_opt_in")
        and item.get("activation") != "session_grant"
    )
    regular = {split_tool_spec(spec)[0] for spec in tools if not is_swarm_tool(spec)}
    missing = sorted(regular - set(allowed))
    if missing:
        raise SwarmError(f"Tools {missing} cannot be selected for a Swarm profile")
    profile = {
        "schema_version": 1,
        "name": f"perf-load {participants} participant(s)",
        "participants": [{"model": CATALOG_MODEL, "count": participants}],
        "working_directory": {"kind": "project", "project_id": project_id},
        "tool_access": {"mode": "selected", "allowed": list(allowed)},
        "instructions": (catalog.get("prompt_defaults") or {}).get("instructions", ""),
    }
    saved = swarm_operation(rpc, "profiles.save", {"profile": profile, "expected_revision": None})
    return SwarmSetup(
        profile_id=str(saved["profile"]["id"]),
        page={"id": SWARM_PAGE_ID, "epoch": str(page["epoch"])},
        allowed_tools=allowed,
    )


@dataclass
class ParticipantHistory:
    """What one participant's Session history shows after the load phase."""

    participant_id: str
    display_name: str | None
    key: str | None = None
    runs: dict[str, str] = field(default_factory=dict)
    tool_calls: dict[str, str] = field(default_factory=dict)
    tool_results: dict[str, tuple[bool | None, float | None]] = field(default_factory=dict)

    @property
    def tool_errors(self) -> int:
        return sum(1 for ok, _duration in self.tool_results.values() if ok is not True)


def parse_history(
    participant_id: str,
    display_name: str | None,
    messages: Sequence[Mapping[str, Any]],
    runs: Mapping[str, str],
) -> ParticipantHistory:
    """Fold ``extensions.page_history`` messages (oldest first) into one record.

    Tool calls are labelled ``name`` or ``name.action`` for Swarm Tools; a Tool
    result counts as ok when its canonical envelope says ``ok: true``. The
    participant's fake-Provider key comes from its first Tool call, the goal
    read.
    """
    history = ParticipantHistory(participant_id, display_name, runs=dict(runs))
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = str(call.get("id", ""))
                if history.key is None:
                    history.key = participant_for_call_id(call_id)
                history.tool_calls[call_id] = _call_label(call)
        elif role == "tool":
            timing = message.get("timing") or {}
            duration = timing.get("duration_ms") if isinstance(timing, dict) else None
            history.tool_results[str(message.get("tool_call_id", ""))] = (
                _envelope_ok(message.get("content")),
                float(duration) if isinstance(duration, int | float) else None,
            )
    return history


def _call_label(call: Mapping[str, Any]) -> str:
    name = str(call.get("name", "?"))
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    action = arguments.get("action") if isinstance(arguments, dict) else None
    return f"{name}.{action}" if name.startswith("swarm_") and isinstance(action, str) else name


def _envelope_ok(content: Any) -> bool | None:
    if not isinstance(content, str):
        return None
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        return None
    return envelope.get("ok") if isinstance(envelope, dict) else None


@dataclass
class SwarmOutcome:
    """How watching one Swarm ended."""

    swarm_id: str
    started_at: float
    finished_at: float
    kicks: int
    polls: int
    wound_down: bool
    final_swarm: dict[str, Any]
    progress: dict[str, Any]


class SwarmRun:
    """One Swarm on the level's server: start, watch, read back, stop."""

    def __init__(
        self,
        rpc: RpcCaller,
        fake: FakeProvider,
        setup: SwarmSetup,
        *,
        participants: int,
        log: Callable[[str], None],
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._rpc = rpc
        self._fake = fake
        self._setup = setup
        self._participants = participants
        self._log = log
        self._poll_seconds = poll_seconds
        self.swarm_id: str | None = None
        self.started_at: float | None = None
        self.stop_result: dict[str, Any] | None = None

    def start(self, goal: str) -> str:
        self.started_at = time.time()
        result = swarm_operation(
            self._rpc,
            "swarms.start",
            {
                "profile_id": self._setup.profile_id,
                "prompt": goal,
                "request_id": f"perf-start-{uuid.uuid4().hex}",
            },
        )
        swarm_id = result.get("swarm_id") or (result.get("swarm") or {}).get("id")
        if not isinstance(swarm_id, str):
            raise SwarmError(f"swarms.start returned no swarm_id: {result}")
        self.swarm_id = swarm_id
        return swarm_id

    def get(self) -> dict[str, Any]:
        result = swarm_operation(self._rpc, "swarms.get", {"swarm_id": self._require_id()})
        return dict(result["swarm"])

    def watch(
        self,
        *,
        turn_budget: int | None,
        deadline: float | None,
        timeout_seconds: float,
    ) -> SwarmOutcome:
        """Poll until the load is done and the Swarm idle; kick a stalled Swarm.

        With ``turn_budget`` the load is done once every participant completed
        that many scripted turns. With a ``deadline`` (monotonic seconds) the
        fake Provider is told to wind down when it passes, and the load is done
        once the in-flight turns finished.
        """
        swarm_id = self._require_id()
        started = time.monotonic()
        limit = started + timeout_seconds
        kicks = polls = idle_polls = stalled_kicks = 0
        wound_down = False
        last_turns = -1
        swarm: dict[str, Any] = {}
        progress: dict[str, Any] = {}
        while True:
            now = time.monotonic()
            if deadline is not None and not wound_down and now >= deadline:
                self._fake.wind_down()
                wound_down = True
                self._log("  duration reached; letting in-flight turns finish")
            swarm = self.get()
            progress = self._fake.progress()
            polls += 1
            participants = swarm.get("participants") or []
            state = swarm.get("state")
            if state in _ATTENTION_STATES:
                raise SwarmError(f"the Swarm entered state {state}: {_participant_states(swarm)}")
            entries = progress.get("participants") or {}
            in_flight = sum(int(entry.get("in_flight") or 0) for entry in entries.values())
            idle = (
                state == "idle"
                and in_flight == 0
                and not any(participant.get("run_active") for participant in participants)
            )
            idle_polls = idle_polls + 1 if idle else 0
            turns = sum(int(entry.get("turns") or 0) for entry in entries.values())
            if turns != last_turns:
                last_turns, stalled_kicks = turns, 0
            if turn_budget is not None:
                done = len(entries) >= self._participants and all(
                    int(entry.get("turns") or 0) >= turn_budget for entry in entries.values()
                )
            else:
                done = wound_down
            if done and idle_polls >= FINISH_IDLE_POLLS:
                break
            if not done and idle_polls >= KICK_IDLE_POLLS:
                if stalled_kicks >= MAX_KICKS_WITHOUT_PROGRESS:
                    raise SwarmError(
                        f"no participant completed a turn after {stalled_kicks} kicks "
                        f"(progress {entries})"
                    )
                kicks += 1
                stalled_kicks += 1
                idle_polls = 0
                self.kick(kicks)
            if now >= limit:
                raise SwarmError(
                    f"the Swarm did not finish within {timeout_seconds:.0f}s "
                    f"(state {state}, progress {entries})"
                )
            time.sleep(self._poll_seconds)
        return SwarmOutcome(
            swarm_id=swarm_id,
            started_at=self.started_at or 0.0,
            finished_at=time.time(),
            kicks=kicks,
            polls=polls,
            wound_down=wound_down,
            final_swarm=swarm,
            progress=progress,
        )

    def kick(self, number: int) -> None:
        swarm_operation(
            self._rpc,
            "board.post",
            {
                "swarm_id": self._require_id(),
                "text": KICK_TEXT.format(number=number),
                "request_id": f"perf-kick-{uuid.uuid4().hex}",
            },
        )

    def histories(self, swarm: Mapping[str, Any]) -> list[ParticipantHistory]:
        """Read every participant's complete history through the page projection."""
        return [
            self._history(str(participant["id"]), participant.get("display_name"))
            for participant in swarm.get("participants") or []
        ]

    def _history(self, participant_id: str, display_name: Any) -> ParticipantHistory:
        messages: dict[int, dict[str, Any]] = {}
        runs: dict[str, str] = {}
        query: dict[str, Any] = {"limit": HISTORY_PAGE_LIMIT}
        for _page in range(MAX_HISTORY_PAGES):
            result = self._rpc.call(
                "extensions.page_history",
                {
                    "name": SWARM_EXTENSION,
                    "page": self._setup.page,
                    "group_id": self._require_id(),
                    "participant_id": participant_id,
                    "query": query,
                },
            )
            for message in result.get("messages") or []:
                messages[int(message["history_sequence"])] = message
            for run in result.get("runs") or []:
                runs[str(run["run_id"])] = str(run.get("status"))
            before = result.get("next_before")
            if not result.get("has_more") or not before:
                break
            query = {"limit": HISTORY_PAGE_LIMIT, "before": before}
        ordered = [messages[sequence] for sequence in sorted(messages)]
        name = display_name if isinstance(display_name, str) else None
        return parse_history(participant_id, name, ordered, runs)

    def stop(self) -> dict[str, Any] | None:
        """Stop the Swarm if one was started; safe to call more than once."""
        if self.swarm_id is None or self.stop_result is not None:
            return self.stop_result
        self.stop_result = dict(
            swarm_operation(
                self._rpc,
                "swarms.stop",
                {"swarm_id": self.swarm_id, "request_id": f"perf-stop-{uuid.uuid4().hex}"},
            )
        )
        return self.stop_result

    def _require_id(self) -> str:
        if self.swarm_id is None:
            raise SwarmError("the Swarm has not been started")
        return self.swarm_id


def _participant_states(swarm: Mapping[str, Any]) -> str:
    return ", ".join(
        f"{participant.get('display_name') or participant.get('id')}={participant.get('state')}"
        for participant in swarm.get("participants") or []
    )


def goal_prompt(directive_text: str) -> str:
    """The Swarm goal: plain instructions plus the fake Provider's directive."""
    return f"{GOAL_TEXT} {directive_text}"
