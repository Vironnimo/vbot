"""Per-Model ratio between Provider-measured and locally estimated request input.

vBot counts every Model's text with one shared local encoding, while Providers
tokenize privately (Claude, Gemini and others count the same text differently).
Each Chat step whose Provider reports its input yields one sample: the measured
input and the local estimate of exactly that request. The ratio of their
exponentially decayed, size-weighted sums corrects later estimates for the same
Model. It absorbs Provider framing and systematic reserve bias as well; it is an
average over recent requests, not a tokenizer.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import NamedTuple

from core.database import Database
from core.utils.timestamps import utc_now_timestamp

# Weight of the previous sums per new sample: about the last fifty requests count.
_DECAY = 0.98
# A Model without samples starts at 1.0; early samples move it only gradually.
_PRIOR_TOKENS = 20_000.0
_MIN_FACTOR = 0.5
_MAX_FACTOR = 2.0
# Tiny requests are dominated by framing; implausible ratios are reporting defects.
_MIN_SAMPLE_ESTIMATE = 1_000
_MAX_SAMPLE_RATIO = 4.0


class _Sums(NamedTuple):
    measured: float
    estimated: float
    samples: int


def model_key(model: str) -> str:
    """The calibrated identity: ``provider/model`` without its Connection scope."""
    return model.split("::", 1)[0]


def _factor(sums: _Sums) -> float:
    ratio = (sums.measured + _PRIOR_TOKENS) / (sums.estimated + _PRIOR_TOKENS)
    return min(_MAX_FACTOR, max(_MIN_FACTOR, ratio))


def is_usable_sample(measured: int, estimated: int) -> bool:
    return (
        estimated >= _MIN_SAMPLE_ESTIMATE
        and measured > 0
        and 1 / _MAX_SAMPLE_RATIO <= measured / estimated <= _MAX_SAMPLE_RATIO
    )


class InputEstimateCalibration:
    """In-memory factors backed by one ``model-usage.db`` row per Model."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with database.read() as connection:
            rows = connection.execute(
                "SELECT model,measured_tokens,estimated_tokens,samples "
                "FROM input_estimate_calibration"
            ).fetchall()
        self._sums = {
            row["model"]: _Sums(row["measured_tokens"], row["estimated_tokens"], row["samples"])
            for row in rows
        }

    def factor(self, model: str) -> float:
        sums = self._sums.get(model_key(model))
        return 1.0 if sums is None else _factor(sums)

    def record(self, model: str, measured: int, estimated: int) -> None:
        """Add one sample; blocking (database write), so call it off the Event Loop."""
        if not is_usable_sample(measured, estimated):
            return
        key = model_key(model)

        def operation(connection: sqlite3.Connection) -> _Sums:
            row = connection.execute(
                "SELECT measured_tokens,estimated_tokens,samples "
                "FROM input_estimate_calibration WHERE model=?",
                (key,),
            ).fetchone()
            previous = (
                _Sums(0.0, 0.0, 0)
                if row is None
                else _Sums(row["measured_tokens"], row["estimated_tokens"], row["samples"])
            )
            sums = _Sums(
                previous.measured * _DECAY + measured,
                previous.estimated * _DECAY + estimated,
                previous.samples + 1,
            )
            connection.execute(
                "INSERT INTO input_estimate_calibration "
                "(model,measured_tokens,estimated_tokens,samples,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET "
                "measured_tokens=excluded.measured_tokens,"
                "estimated_tokens=excluded.estimated_tokens,"
                "samples=excluded.samples,updated_at=excluded.updated_at",
                (key, sums.measured, sums.estimated, sums.samples, utc_now_timestamp()),
            )
            return sums

        sums = self._database.write(operation)
        with self._lock:
            # Concurrent Runs may finish their writes out of order.
            current = self._sums.get(key)
            if current is None or sums.samples > current.samples:
                self._sums[key] = sums
