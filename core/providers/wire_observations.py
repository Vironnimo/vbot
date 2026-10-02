"""Wire facts learned from live Provider traffic (wire profile layer 7).

Codecs report what a Provider actually did for one (Provider, Connection,
Model): which readable field carried reasoning, whether reasoning came back at
all, which optional parameter the wire rejected, which effort value it refused
(``none`` also for a refused explicit off). The resolver turns these facts
into profile values below every explicit Model entry, so a configured or
verified profile always wins and an unconfigured Model improves after its first
responses instead of failing the same way on every request.

The store is a disposable cache, not a durable document: it lives at
``<data-dir>/artifacts/wire-observations.json``, an unreadable or foreign file
starts empty, and writes are debounced atomic replaces off the caller's thread.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

_LOGGER = get_logger("providers.wire_observations")

OBSERVATIONS_FORMAT_VERSION = 1
OBSERVATIONS_FILE_NAME = "wire-observations.json"
_DEFAULT_SAVE_DELAY_SECONDS = 2.0
_MAX_LIST_ENTRIES = 16


@dataclass(frozen=True)
class ObservedFacts:
    """What live traffic showed for one (Provider, Connection, Model)."""

    reasoning_field: str | None = None
    rejected_parameters: tuple[str, ...] = ()
    rejected_efforts: tuple[str, ...] = ()
    reasoning_returned: bool = False

    def is_empty(self) -> bool:
        return self == _NO_FACTS


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
    ) -> None:
        self._path = path
        self._save_delay = save_delay
        self._logger = logger if logger is not None else _LOGGER
        self._facts: dict[str, ObservedFacts] = {}
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
    ) -> WireObservations:
        store = cls(path, save_delay=save_delay, logger=logger)
        if path is not None:
            store._facts = _read(path, store._logger)
        return store

    @property
    def generation(self) -> int:
        """Changes whenever a recorded fact changes (resolved profiles key on it)."""

        return self._generation

    def facts_for(self, provider_id: str, connection_id: str, model_id: str) -> ObservedFacts:
        return self._facts.get(_key(provider_id, connection_id, model_id), _NO_FACTS)

    def snapshot(self) -> Mapping[str, ObservedFacts]:
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
        )

    def forget(self, provider_id: str, connection_id: str | None = None) -> None:
        """Drop the facts of a Provider (or one of its Connections)."""

        prefix = f"{provider_id}|" if connection_id is None else f"{provider_id}|{connection_id}|"
        with self._lock:
            removed = [key for key in self._facts if key.startswith(prefix)]
            if not removed:
                return
            for key in removed:
                del self._facts[key]
            self._generation += 1
            self._dirty = True
        self._schedule_save()

    def _update(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        change: Callable[[ObservedFacts], ObservedFacts],
        description: str | None,
    ) -> None:
        key = _key(provider_id, connection_id, model_id)
        with self._lock:
            current = self._facts.get(key, _NO_FACTS)
            updated = change(current)
            if updated == current:
                return
            self._facts[key] = updated
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
            document = _document(self._facts)
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


def _document(facts: Mapping[str, ObservedFacts]) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for key, item in sorted(facts.items()):
        entry: dict[str, Any] = {}
        if item.reasoning_field:
            entry["reasoning_field"] = item.reasoning_field
        if item.rejected_parameters:
            entry["rejected_parameters"] = list(item.rejected_parameters)
        if item.rejected_efforts:
            entry["rejected_efforts"] = list(item.rejected_efforts)
        if item.reasoning_returned:
            entry["reasoning_returned"] = True
        if entry:
            targets[key] = entry
    return {"format_version": OBSERVATIONS_FORMAT_VERSION, "targets": targets}


def _read(path: Path, logger: Any) -> dict[str, ObservedFacts]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable wire observations %s: %s", path, exc)
        return {}
    if not isinstance(raw, Mapping) or raw.get("format_version") != OBSERVATIONS_FORMAT_VERSION:
        logger.warning("Ignoring wire observations %s with an unknown format", path)
        return {}
    targets = raw.get("targets")
    if not isinstance(targets, Mapping):
        return {}
    facts: dict[str, ObservedFacts] = {}
    for key, entry in targets.items():
        if not isinstance(key, str) or key.count("|") != 2 or not isinstance(entry, Mapping):
            continue
        field = entry.get("reasoning_field")
        facts[key] = ObservedFacts(
            reasoning_field=field if isinstance(field, str) and field else None,
            rejected_parameters=_strings(entry.get("rejected_parameters")),
            rejected_efforts=_strings(entry.get("rejected_efforts")),
            reasoning_returned=entry.get("reasoning_returned") is True,
        )
    return facts


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: Iterable[Any] = value[:_MAX_LIST_ENTRIES]
    return tuple(item for item in items if isinstance(item, str) and item)
