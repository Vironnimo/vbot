"""Request shaping from a wire profile and the catalog Model.

The shared Responses codec (:func:`build_responses_payload`) takes a request
policy. A profile-driven Responses wire derives that policy from its wire
profile and the catalog Model: the Tool, parallel-Tool and structured-output
support the catalog reports (:func:`catalog_tool_support`; the request option
``structured_outputs`` widens structured output to every Model), and the
optional parameters the profile's ``request.allowed_parameters`` admits. Reasoning is not
part of the policy: :func:`take_reasoning_renderer` consumes the caller's
reasoning kwargs, the profile plans them, and its reasoning dialect renders the
plan. :func:`drop_unsupported_request_kwargs` applies the same catalog and
parameter filter to the request kwargs of any other wire.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from core.models.models import Model
from core.providers._chat_completions_wire import _selected_thinking_effort
from core.providers._openai_constants import (
    OPTIONAL_REQUEST_PARAMETER_NAMES,
    REASONING_PARAMETER_NAMES,
    STRUCTURED_OUTPUT_PARAMETER_NAMES,
    TOOL_PARAMETER_NAMES,
)
from core.providers.reasoning_dialects import render_reasoning
from core.providers.wire_profile import WireProfile

__all__ = [
    "RESPONSES_DOCUMENT_TYPES",
    "RESPONSES_REASONING_FIELDS",
    "CatalogToolSupport",
    "ProfileResponsesPolicy",
    "catalog_tool_support",
    "drop_unsupported_request_kwargs",
    "profile_responses_policy",
    "take_reasoning_renderer",
]

RESPONSES_OPTIONAL_PARAMETERS = OPTIONAL_REQUEST_PARAMETER_NAMES | {
    "prompt_cache_key",
    "service_tier",
}
"""Optional parameters the Responses codec forwards when the profile lists none."""

RESPONSES_REASONING_FIELDS = ("reasoning", "include")
"""Request fields the ``responses_reasoning`` dialect writes."""

RESPONSES_DOCUMENT_TYPES = frozenset({"application/pdf"})
"""Document media the Responses codec can carry as ``input_file`` parts."""


class CatalogToolSupport(TypedDict):
    """The Tool and structured-output support of a Model (a request policy's Tool fields)."""

    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_structured_outputs: bool


@dataclass(frozen=True)
class ProfileResponsesPolicy:
    """The request policy of a profile-driven Responses wire.

    It filters Tools, structured output and optional parameters; it carries
    no reasoning controls because the profile's plan renders reasoning.
    """

    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_structured_outputs: bool
    supported_request_parameters: frozenset[str]

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered = drop_unsupported_request_kwargs(
            kwargs,
            support={
                "supports_tools": self.supports_tools,
                "supports_parallel_tool_calls": self.supports_parallel_tool_calls,
                "supports_structured_outputs": self.supports_structured_outputs,
            },
            allowed_parameters=self.supported_request_parameters,
        )
        for name in REASONING_PARAMETER_NAMES:
            filtered.pop(name, None)
        return filtered

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name in self.supported_request_parameters


def catalog_tool_support(model: Model | None) -> CatalogToolSupport:
    """Tool, parallel-Tool and structured-output support from the catalog.

    A Model the catalog does not know is assumed to support all three.
    Parallel Tool calls need Tools and, when the catalog lists supported
    parameters, ``parallel_tool_calls`` or ``tools`` among them.
    """

    if model is None:
        return {
            "supports_tools": True,
            "supports_parallel_tool_calls": True,
            "supports_structured_outputs": True,
        }
    capabilities = model.capabilities
    supported_parameters = set(capabilities.supported_parameters)
    return {
        "supports_tools": capabilities.tools,
        "supports_parallel_tool_calls": capabilities.tools
        and (
            not supported_parameters
            or "parallel_tool_calls" in supported_parameters
            or "tools" in supported_parameters
        ),
        "supports_structured_outputs": capabilities.json_mode,
    }


def profile_responses_policy(profile: WireProfile, model: Model | None) -> ProfileResponsesPolicy:
    """The request policy of ``model`` on a profile-driven Responses wire.

    Tool support follows the catalog (:func:`catalog_tool_support`); the
    request option ``structured_outputs: true`` declares structured output
    for every Model of the wire, whatever the catalog's ``json_mode`` says.
    """

    support = catalog_tool_support(model)
    if profile.request.options.get("structured_outputs") is True:
        support["supports_structured_outputs"] = True
    return ProfileResponsesPolicy(
        **support,
        supported_request_parameters=_allowed_request_parameters(profile, model),
    )


def drop_unsupported_request_kwargs(
    kwargs: Mapping[str, Any],
    *,
    support: CatalogToolSupport,
    allowed_parameters: Collection[str],
) -> dict[str, Any]:
    """Return ``kwargs`` without the request features the Model or wire does not take.

    Tools (with ``tool_choice`` and ``parallel_tool_calls``), parallel Tool
    calls and structured output follow ``support``; an optional parameter
    (output limits and sampling) is kept only when ``allowed_parameters``
    names it. Other kwargs pass through.
    """

    filtered = dict(kwargs)
    if not support["supports_tools"]:
        for name in TOOL_PARAMETER_NAMES:
            filtered.pop(name, None)
    elif not support["supports_parallel_tool_calls"]:
        filtered.pop("parallel_tool_calls", None)
    if not support["supports_structured_outputs"]:
        for name in STRUCTURED_OUTPUT_PARAMETER_NAMES:
            filtered.pop(name, None)
    for name in OPTIONAL_REQUEST_PARAMETER_NAMES:
        if name in filtered and name not in allowed_parameters:
            filtered.pop(name, None)
    return filtered


def _allowed_request_parameters(profile: WireProfile, model: Model | None) -> frozenset[str]:
    """The optional request parameters the profile allows for ``model``.

    ``request.allowed_parameters`` lists them (``None``: every optional
    parameter the Responses codec knows). With the request option
    ``narrow_to_catalog_parameters`` the list narrows to the parameters the
    catalog's ``supported_parameters`` names, when it names any;
    ``max_tokens`` counts as named with ``max_output_tokens``.
    """

    rules = profile.request
    allowed = (
        frozenset(rules.allowed_parameters)
        if rules.allowed_parameters is not None
        else RESPONSES_OPTIONAL_PARAMETERS
    )
    if rules.options.get("narrow_to_catalog_parameters") is not True:
        return allowed
    advertised = (
        frozenset(model.capabilities.supported_parameters) if model is not None else frozenset()
    )
    if not advertised:
        return allowed
    return frozenset(
        parameter
        for parameter in allowed
        if parameter in advertised
        or (parameter == "max_tokens" and "max_output_tokens" in advertised)
    )


def take_reasoning_renderer(
    profile: WireProfile, request_kwargs: dict[str, Any]
) -> Callable[[dict[str, Any]], None]:
    """Consume the caller's reasoning kwargs and plan them on the profile.

    The returned renderer spells the plan in the profile's reasoning dialect.
    """

    effort = _selected_thinking_effort(request_kwargs)
    for name in REASONING_PARAMETER_NAMES:
        request_kwargs.pop(name, None)
    wire = profile.reasoning
    intent = wire.plan(effort)
    return lambda payload: render_reasoning(wire, intent, payload)
