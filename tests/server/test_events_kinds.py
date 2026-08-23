"""Tests for the resource-changed kind contract additions."""

from server.events import (
    ALLOWED_RESOURCE_KINDS,
    RESOURCE_CHANGED_EVENT,
    RESOURCE_KIND_SKILLS,
    ServerEventBus,
)


def test_skills_is_a_allowed_resource_kind() -> None:
    assert RESOURCE_KIND_SKILLS == "skills"
    assert RESOURCE_KIND_SKILLS in ALLOWED_RESOURCE_KINDS


def test_publish_accepts_the_skills_kind() -> None:
    bus = ServerEventBus()

    event = bus.publish(RESOURCE_CHANGED_EVENT, {"kind": RESOURCE_KIND_SKILLS})

    assert event["payload"] == {"kind": "skills"}
