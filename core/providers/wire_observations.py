"""Wire facts learned from live Provider traffic (wire profile layer 7).

Codecs report what a Provider actually did for one (Provider, Connection,
Model): which readable field carried reasoning, whether reasoning came back at
all, which optional parameter the wire rejected, which parameters it accepts
only one of at a time, which effort value it refused (``none`` also for a
refused explicit off). The resolver turns these facts
into profile values below every explicit Model entry, so a configured or
verified profile always wins and an unconfigured Model improves after its first
responses instead of failing the same way on every request.

A rejection is not forever: each rejected parameter, parameter group or effort expires
``REJECTION_TTL`` after it was learned, so a one-off or misattributed rejection
heals by itself (the learner retries a rejected request at once, so re-learning
a rejection that still holds costs one extra request). ``forget`` drops facts
on request (``model.forget_wire_facts``).

The store is a disposable cache, not a durable document: it lives at
``<data-dir>/artifacts/wire-observations.json``, an unreadable or foreign file
starts empty, and writes are debounced atomic replaces off the caller's thread.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

_LOGGER = get_logger("providers.wire_observations")

OBSERVATIONS_FORMAT_VERSION = 2
OBSERVATIONS_FILE_NAME = "wire-observations.json"
REJECTION_TTL = timedelta(days=30)
EXCLUSIVE_GROUP_SEPARATOR = "+"
_DEFAULT_SAVE_DELAY_SECONDS = 2.0
_MAX_LIST_ENTRIES = 16

Clock = Callable[[], datetime]
# A rejection's identity within one target: ("parameter" | "exclusive" | "effort", name);
# an exclusive group's name joins its parameters with ``+`` in the order kept first.
_Rejection = tuple[str, str]
_RejectionTimes = dict[str, dict[_Rejection, datetime]]


@dataclass(frozen=True)
class ObservedFacts:
    """What live traffic showed for one (Provider, Connection, Model)."""

    reasoning_field: str | None = None
    rejected_parameters: tuple[str, ...] = ()
    exclusive_parameters: tuple[str, ...] = ()
    """Groups the wire accepts only one parameter of, each ``+``-joined, the kept one first."""
    rejected_efforts: tuple[str, ...] = ()
    reasoning_returned: bool = False

    def is_empty(self) -> bool:
        return self == _NO_FACTS

    def without(self, rejection: _Rejection) -> ObservedFacts:
        kind, name = rejection
        if kind == "parameter":
            return replace(
                self,
                rejected_parameters=tuple(
                    item for item in self.rejected_parameters if item != name
                ),
            )
        if kind == "exclusive":
            return replace(
                self,
                exclusive_parameters=tuple(
                    item for item in self.exclusive_parameters if item != name
                ),
            )
        return replace(
            self, rejected_efforts=tuple(item for item in self.rejected_efforts if item != name)
        )


_NO_FACTS = ObservedFacts()


def _key(provider_id: str, connection_id: str, model_id: str) -> str:
    return f"{provider_id}|{connection_id}|{model_id.split('::', 1)[0]}"


class WireObservations:
    """In-memory observation store with a debounced on-disk cache."""

    def __init__(
        self,
        path: Path | None,
        *,
        save_delay: float | None = _DEFAULT_SAVE_DELAY_SECONDS,
        logger: Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._path = path
        self._save_delay = save_delay
        self._logger = logger if logger is not None else _LOGGER
        self._clock: Clock = clock if clock is not None else _utc_now
        self._facts: dict[str, ObservedFacts] = {}
        self._learned_at: _RejectionTimes = {}
        self._next_expiry: datetime | None = None
        self._generation = 0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._dirty = False

    @classmethod
    def load(
        cls,
        path: Path | None,
        *,
        save_delay: float | None = _DEFAULT_SAVE_DELAY_SECONDS,
        logger: Any | None = None,
        clock: Clock | None = None,
    ) -> WireObservations:
        store = cls(path, save_delay=save_delay, logger=logger, clock=clock)
        if path is not None:
            store._facts, store._learned_at = _read(path, store._logger, store._clock())
            store._next_expiry = _next_expiry(store._learned_at)
            store._expire()
        return store

    @property
    def generation(self) -> int:
        """Changes whenever a fact changes or expires (resolved profiles key on it)."""

        self._expire()
        return self._generation

    def facts_for(self, provider_id: str, connection_id: str, model_id: str) -> ObservedFacts:
        self._expire()
        return self._facts.get(_key(provider_id, connection_id, model_id), _NO_FACTS)

    def snapshot(self) -> Mapping[str, ObservedFacts]:
        self._expire()
        with self._lock:
            return dict(self._facts)

    # -- recording -------------------------------------------------------------

    def record_reasoning_field(
        self, provider_id: str, connection_id: str, model_id: str, field: str
    ) -> None:
        """The readable reasoning of a response arrived in ``field``."""

        if not field:
            return
        self._update(
            provider_id,
            connection_id,
            model_id,
            lambda facts: replace(facts, reasoning_field=field, reasoning_returned=True),
            f"reasoning arrives in {field!r}",
        )

    def record_reasoning_returned(
        self, provider_id: str, connection_id: str, model_id: str
    ) -> None:
        """A reasoning request returned reasoning (text, opaque state or tokens)."""

        self._update(
            provider_id,
            connection_id,
            model_id,
            lambda facts: replace(facts, reasoning_returned=True),
            None,
        )

    def record_rejected_parameter(
        self, provider_id: str, connection_id: str, model_id: str, parameter: str
    ) -> None:
        """The wire rejected the optional request parameter ``parameter``."""

        self._update(
            provider_id,
            connection_id,
            model_id,
            lambda facts: replace(
                facts, rejected_parameters=_with(facts.rejected_parameters, parameter)
            ),
            f"the wire rejects parameter {parameter!r}",
            rejection=("parameter", parameter),
        )

    def record_exclusive_parameters(
        self, provider_id: str, connection_id: str, model_id: str, group: Sequence[str]
    ) -> None:
        """The wire accepts only one of ``group`` per request; the first is the one kept."""

        name = EXCLUSIVE_GROUP_SEPARATOR.join(group)
        self._update(
            provider_id,
            connection_id,
            model_id,
            lambda facts: replace(
                facts, exclusive_parameters=_with(facts.exclusive_parameters, name)
            ),
            f"the wire accepts only one of {', '.join(repr(item) for item in group)}",
            rejection=("exclusive", name),
        )

    def record_rejected_effort(
        self, provider_id: str, connection_id: str, model_id: str, effort: str
    ) -> None:
        """The wire rejected the reasoning effort value ``effort``.

        ``none`` also records a rejected explicit off (``thinking: {type:
        disabled}``), which the resolver turns into an omitted off render.
        """

        self._update(
            provider_id,
            connection_id,
            model_id,
            lambda facts: replace(facts, rejected_efforts=_with(facts.rejected_efforts, effort)),
            f"the wire rejects reasoning effort {effort!r}",
            rejection=("effort", effort),
        )

    def forget(
        self,
        provider_id: str,
        connection_id: str | None = None,
        model_id: str | None = None,
    ) -> int:
        """Drop the facts of a Provider, optionally narrowed to one Connection and Model.

        Returns how many (Connection, Model) targets lost their facts.
        """

        bare_model = model_id.split("::", 1)[0] if model_id is not None else None

        def selected(key: str) -> bool:
            provider, connection, model = key.split("|", 2)
            return (
                provider == provider_id
                and (connection_id is None or connection == connection_id)
                and (bare_model is None or model == bare_model)
            )

        with self._lock:
            removed = [key for key in self._facts if selected(key)]
            if not removed:
                return 0
            for key in removed:
                del self._facts[key]
                self._learned_at.pop(key, None)
            self._next_expiry = _next_expiry(self._learned_at)
            self._generation += 1
            self._dirty = True
        self._logger.info(
            "Forgot learned wire facts (provider=%s connection=%s model=%s targets=%s)",
            provider_id,
            connection_id or "*",
            bare_model or "*",
            len(removed),
        )
        self._schedule_save()
        return len(removed)

    def _expire(self) -> None:
        """Drop every rejection learned ``REJECTION_TTL`` or longer ago."""

        next_expiry = self._next_expiry
        if next_expiry is None:
            return
        now = self._clock()
        if now < next_expiry:
            return
        cutoff = now - REJECTION_TTL
        with self._lock:
            expired = [
                (key, rejection)
                for key, rejections in self._learned_at.items()
                for rejection, learned_at in rejections.items()
                if learned_at <= cutoff
            ]
            for key, rejection in expired:
                rejections = self._learned_at[key]
                del rejections[rejection]
                if not rejections:
                    del self._learned_at[key]
                facts = self._facts.get(key, _NO_FACTS).without(rejection)
                if facts.is_empty():
                    self._facts.pop(key, None)
                else:
                    self._facts[key] = facts
            self._next_expiry = _next_expiry(self._learned_at)
            if not expired:
                return
            self._generation += 1
            self._dirty = True
        self._logger.info(
            "Expired %s learned wire rejection(s) after %s days",
            len(expired),
            REJECTION_TTL.days,
        )
        self._schedule_save()

    def _update(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        change: Callable[[ObservedFacts], ObservedFacts],
        description: str | None,
        *,
        rejection: _Rejection | None = None,
    ) -> None:
        key = _key(provider_id, connection_id, model_id)
        with self._lock:
            current = self._facts.get(key, _NO_FACTS)
            updated = change(current)
            if updated == current:
                return
            self._facts[key] = updated
            if rejection is not None:
                times = self._learned_at.setdefault(key, {})
                times[rejection] = self._clock()
                # ``_with`` keeps the newest entries only; forget the evicted ones' times.
                kept = (
                    {("parameter", name) for name in updated.rejected_parameters}
                    | {("exclusive", name) for name in updated.exclusive_parameters}
                    | {("effort", name) for name in updated.rejected_efforts}
                )
                for evicted in [item for item in times if item not in kept]:
                    del times[evicted]
                self._next_expiry = _next_expiry(self._learned_at)
            self._generation += 1
            self._dirty = True
        if description is not None:
            self._logger.info(
                "Learned wire fact for %s:%s model %s: %s",
                provider_id,
                connection_id,
                model_id.split("::", 1)[0],
                description,
            )
        self._schedule_save()

    # -- persistence -------------------------------------------------------------

    def _schedule_save(self) -> None:
        if self._path is None or self._save_delay is None:
            return
        with self._lock:
            if self._timer is not None:
                return
            timer = threading.Timer(self._save_delay, self.flush)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def flush(self) -> None:
        """Write pending changes now (best effort; a failure is logged)."""

        with self._lock:
            self._timer = None
            if not self._dirty or self._path is None:
                return
            document = _document(self._facts, self._learned_at)
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._path, json.dumps(document, indent=1, sort_keys=True))
        except OSError as exc:
            self._logger.warning("Could not save wire observations to %s: %s", self._path, exc)

    def close(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
        self.flush()


def _with(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    if not value or value in values:
        return values
    return (*values, value)[-_MAX_LIST_ENTRIES:]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _next_expiry(learned_at: _RejectionTimes) -> datetime | None:
    times = [at for rejections in learned_at.values() for at in rejections.values()]
    return min(times) + REJECTION_TTL if times else None


def _document(facts: Mapping[str, ObservedFacts], learned_at: _RejectionTimes) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for key, item in sorted(facts.items()):
        times = learned_at.get(key, {})
        entry: dict[str, Any] = {}
        if item.reasoning_field:
            entry["reasoning_field"] = item.reasoning_field
        if item.rejected_parameters:
            entry["rejected_parameters"] = {
                name: _stamp(times.get(("parameter", name))) for name in item.rejected_parameters
            }
        if item.exclusive_parameters:
            entry["exclusive_parameters"] = {
                name: _stamp(times.get(("exclusive", name))) for name in item.exclusive_parameters
            }
        if item.rejected_efforts:
            entry["rejected_efforts"] = {
                name: _stamp(times.get(("effort", name))) for name in item.rejected_efforts
            }
        if item.reasoning_returned:
            entry["reasoning_returned"] = True
        if entry:
            targets[key] = entry
    return {"format_version": OBSERVATIONS_FORMAT_VERSION, "targets": targets}


def _stamp(value: datetime | None) -> str:
    return (value if value is not None else _utc_now()).isoformat()


def _read(
    path: Path, logger: Any, now: datetime
) -> tuple[dict[str, ObservedFacts], _RejectionTimes]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable wire observations %s: %s", path, exc)
        return {}, {}
    if not isinstance(raw, Mapping) or raw.get("format_version") != OBSERVATIONS_FORMAT_VERSION:
        logger.warning("Ignoring wire observations %s with an unknown format", path)
        return {}, {}
    targets = raw.get("targets")
    if not isinstance(targets, Mapping):
        return {}, {}
    facts: dict[str, ObservedFacts] = {}
    learned_at: _RejectionTimes = {}
    for key, entry in targets.items():
        if not isinstance(key, str) or key.count("|") != 2 or not isinstance(entry, Mapping):
            continue
        field = entry.get("reasoning_field")
        parameters = _rejections(entry.get("rejected_parameters"), now)
        groups = {
            name: at
            for name, at in _rejections(entry.get("exclusive_parameters"), now).items()
            if len(name.split(EXCLUSIVE_GROUP_SEPARATOR)) >= 2
        }
        efforts = _rejections(entry.get("rejected_efforts"), now)
        times: dict[_Rejection, datetime] = {
            ("parameter", name): at for name, at in parameters.items()
        }
        times.update({("exclusive", name): at for name, at in groups.items()})
        times.update({("effort", name): at for name, at in efforts.items()})
        if times:
            learned_at[key] = times
        facts[key] = ObservedFacts(
            reasoning_field=field if isinstance(field, str) and field else None,
            rejected_parameters=tuple(parameters),
            exclusive_parameters=tuple(groups),
            rejected_efforts=tuple(efforts),
            reasoning_returned=entry.get("reasoning_returned") is True,
        )
    return facts, learned_at


def _rejections(value: Any, now: datetime) -> dict[str, datetime]:
    """Read ``{name: learned_at}``; a missing or unreadable time counts as learned now."""

    if not isinstance(value, Mapping):
        return {}
    rejections: dict[str, datetime] = {}
    for name, stamp in list(value.items())[:_MAX_LIST_ENTRIES]:
        if not isinstance(name, str) or not name:
            continue
        learned_at = now
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None:
                learned_at = parsed
        rejections[name] = learned_at
    return rejections
