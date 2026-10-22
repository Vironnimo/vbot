"""Model data classes and registry.

Exports load on first access, so importing a light member such as
``MODEL_TASK_ORDER`` does not import the registry and its worker pool.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.models._model_record import (
        MODEL_TASK_ORDER,
        REASONING_CONTROL_BUDGET,
        REASONING_CONTROL_ON_OFF,
        Capabilities,
        Model,
        ReasoningCapabilities,
        derive_model_task_types,
    )
    from core.models.models import ModelRegistry
    from core.models.query import ModelQuery


_EXPORT_MODULES = {
    "Capabilities": "core.models._model_record",
    "Model": "core.models._model_record",
    "ModelQuery": "core.models.query",
    "ModelRegistry": "core.models.models",
    "MODEL_TASK_ORDER": "core.models._model_record",
    "REASONING_CONTROL_BUDGET": "core.models._model_record",
    "REASONING_CONTROL_ON_OFF": "core.models._model_record",
    "ReasoningCapabilities": "core.models._model_record",
    "derive_model_task_types": "core.models._model_record",
}


def __getattr__(name: str) -> Any:
    """Load public model exports only when a caller requests them."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "Capabilities",
    "Model",
    "ModelQuery",
    "ModelRegistry",
    "MODEL_TASK_ORDER",
    "REASONING_CONTROL_BUDGET",
    "REASONING_CONTROL_ON_OFF",
    "ReasoningCapabilities",
    "derive_model_task_types",
]
