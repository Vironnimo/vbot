"""Optional Decision Model review of prepared shell commands."""

from __future__ import annotations

from core.extensions import ExtensionAPI

from ._review import (
    DEFAULT_POLICY,
    DEFAULT_THRESHOLD,
    DEFAULT_TIMEOUT,
    REVIEW_PARAMETERS,
    ShellReview,
)


def register(api: ExtensionAPI) -> None:
    review = ShellReview(api.get_config, api.logger)
    api.register_settings(
        [
            {
                "key": "enabled",
                "type": "toggle",
                "label": "Review shell commands",
                "description": "Use the selected Decision Model before shell execution.",
                "default": False,
            },
            {
                "key": "policy",
                "type": "textarea",
                "label": "Review criteria",
                "description": "Criteria sent to the Decision Model for each command.",
                "default": DEFAULT_POLICY,
            },
            {
                "key": "allow_threshold",
                "type": "number",
                "label": "Automatic approval threshold",
                "description": (
                    "Allow probability required to proceed without confirmation (0.5 to 1). "
                    "Probabilities are Model estimates, not accuracy guarantees."
                ),
                "default": DEFAULT_THRESHOLD,
            },
            {
                "key": "timeout_seconds",
                "type": "number",
                "label": "Review timeout in seconds",
                "description": "Ask for confirmation when review exceeds this time (0.1 to 120).",
                "default": DEFAULT_TIMEOUT,
            },
        ]
    )
    api.operations.startup.append(review.start)
    api.on_shutdown(review.close)
    api.register_shell_reviewer(
        review.prepared, active=lambda: api.get_config().get("enabled", False) is not False
    )
    api.operations.register(
        "review",
        "Preview a shell authorization judgment without executing the command.",
        REVIEW_PARAMETERS,
        review.preview,
    )
