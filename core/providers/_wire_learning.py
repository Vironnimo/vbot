"""Learn wire facts from a Provider's request rejections and retry once per lesson.

A Model that vBot has not verified can reject an optional request parameter
(``temperature`` on a reasoning Model) or a reasoning effort value (``minimal``
where only ``low`` exists). Instead of failing every later request the same
way, the codec records the rejection as a learned wire fact, rebuilds the
request from the updated wire profile and retries. Learned facts feed the
profile below every explicit Model entry, so later requests start from the
corrected shape and a configured profile is never silently overridden.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from logging import Logger
from typing import Any

from core.providers._http_shared import unsupported_sampling_parameter
from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.reasoning import detail_names_rejected_effort
from core.providers.wire_profiles import WireBinding

__all__ = ["execute_learning_from_rejections"]

_MAX_LESSONS = 2
"""At most one parameter and one effort lesson per request."""


async def execute_learning_from_rejections[T](
    execute_attempt: Callable[[], Awaitable[T]],
    payload: dict[str, Any],
    *,
    rebuild: Callable[[], dict[str, Any]],
    sent_effort: Callable[[], str | None],
    wire: WireBinding,
    model_id: str,
    logger: Logger,
    provider_label: str,
) -> T:
    """Run one request, learning from rejected parameters and efforts.

    ``execute_attempt`` performs one full (``retry_async``-wrapped) request over
    the shared ``payload`` dict. On a fatal, non-auth ``ProviderError`` whose
    detail blames a sampling parameter present in ``payload`` or the reasoning
    effort the request carried, the fact is recorded, ``payload`` is replaced by
    ``rebuild()`` and the request runs again. Parameters rejected during this
    request stay removed even when an explicit Model entry keeps sending them.
    Every other error, and a rebuild that changes nothing, propagates unchanged.
    """

    rejected_parameters: set[str] = set()
    for _ in range(_MAX_LESSONS):
        try:
            return await execute_attempt()
        except ProviderError as error:
            if error.retryable or isinstance(error, ProviderAuthError):
                raise
            lesson = _learn(str(error), payload, sent_effort, wire, model_id)
            if lesson is None:
                raise
            if lesson[0] == "parameter":
                rejected_parameters.add(lesson[1])
            rebuilt = rebuild()
            for parameter in rejected_parameters:
                rebuilt.pop(parameter, None)
            if rebuilt == payload:
                raise
            logger.warning(
                "%s rejected %s %r for %s; retrying with the learned wire shape",
                provider_label,
                lesson[0],
                lesson[1],
                model_id,
            )
            payload.clear()
            payload.update(rebuilt)
    return await execute_attempt()


def _learn(
    detail: str,
    payload: dict[str, Any],
    sent_effort: Callable[[], str | None],
    wire: WireBinding,
    model_id: str,
) -> tuple[str, str] | None:
    parameter = unsupported_sampling_parameter(detail)
    if parameter is not None and parameter in payload:
        wire.observe_rejected_parameter(model_id, parameter)
        return "parameter", parameter
    if not detail_names_rejected_effort(detail):
        return None
    # Only a rejection that names the value this request sent is attributable;
    # anything vaguer must not teach every later request a narrower wire.
    effort = sent_effort()
    if effort is not None and _names_value(detail, effort):
        wire.observe_rejected_effort(model_id, effort)
        return "effort", effort
    return None


def _names_value(detail: str, value: str) -> bool:
    return re.search(rf"(?<![a-z_]){re.escape(value)}(?![a-z_])", detail.lower()) is not None
