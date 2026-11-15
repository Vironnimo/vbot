"""Shared public RPC parameter contracts and Extension exposure decisions.

The canonical RPC field gate and Extension schemas read this same owner. Domain
handlers retain value validation, authorization, mutation ordering and results.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from . import content, conversation, system
from .common import ServiceDefinition

SERVICE_DEFINITIONS: tuple[ServiceDefinition, ...] = (
    *conversation.SERVICE_DEFINITIONS,
    *content.SERVICE_DEFINITIONS,
    *system.SERVICE_DEFINITIONS,
)
SERVICE_EXCLUSIONS: dict[str, str] = {
    **conversation.SERVICE_EXCLUSIONS,
    **content.SERVICE_EXCLUSIONS,
    **system.SERVICE_EXCLUSIONS,
}


@dataclass(frozen=True)
class RpcParameterContract:
    """A shared wire envelope with explicitly narrower Extension exposure."""

    service: ServiceDefinition
    rpc_only_fields: Mapping[str, str]

    @property
    def name(self) -> str:
        return self.service[0]

    @property
    def request_fields(self) -> frozenset[str]:
        return frozenset(self.service[2]["properties"]) | self.rpc_only_fields.keys()


def parameter_contracts() -> dict[str, RpcParameterContract]:
    """Build shared field envelopes; bad exposure decisions fail at startup."""
    rpc_only: dict[str, dict[str, str]] = {}
    for family in (conversation, content, system):
        for name, fields in family.RPC_ONLY_FIELDS.items():
            if name in rpc_only:
                raise ValueError(f"Duplicate RPC-only field declaration: {name}")
            rpc_only[name] = fields
    result = {}
    for service in SERVICE_DEFINITIONS:
        name, _kind, schema, _resources = service
        if name in result:
            raise ValueError(f"Duplicate RPC parameter contract: {name}")
        extras = dict(rpc_only.pop(name, {}))
        if schema.get("additionalProperties") is not False or not isinstance(
            schema.get("properties"), dict
        ):
            raise ValueError(f"RPC parameter contract must have explicit fields: {name}")
        if extras.keys() & schema["properties"].keys() or any(
            not field or not reason.strip() for field, reason in extras.items()
        ):
            raise ValueError(f"RPC-only fields need distinct names and reasons: {name}")
        result[name] = RpcParameterContract(service, MappingProxyType(extras))
    if rpc_only:
        raise ValueError(f"RPC-only fields have no contract: {sorted(rpc_only)}")
    return result


PARAMETER_CONTRACTS = parameter_contracts()


def request_fields(method: str) -> frozenset[str]:
    """Return the field contract used by both transports, never inferred fields."""
    try:
        return PARAMETER_CONTRACTS[method].request_fields
    except KeyError as exc:
        raise ValueError(f"RPC has no shared parameter contract: {method}") from exc
