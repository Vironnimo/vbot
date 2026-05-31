"""Settings and task-model RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return settings and task-model RPC handlers from the delegates facade."""

    return {
        "settings.get_raw": delegates._get_settings_raw,
        "settings.set_key": delegates._set_settings_key,
        "settings.get": delegates._get_settings,
        "settings.update": delegates._update_settings,
        "task_model.settings": delegates._task_model_settings,
        "task_model.update": delegates._task_model_update,
        "task_model.list_targets": delegates._task_model_list_targets,
        "task_model.options": delegates._task_model_options,
    }
