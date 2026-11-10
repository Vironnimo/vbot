"""UIA contracts with in-memory providers and pipes; no real desktop or subprocesses."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import _uia_worker, _win_accessibility
from resources.extensions.computer_use.target import InputInterrupted, TargetError


@pytest.mark.parametrize("operation", ["inspect", "act"])
@pytest.mark.parametrize("stopped", [False, True])
def test_uia_deadline_and_stop_cover_a_blocked_request_pipe(monkeypatch, operation, stopped):
    killed = threading.Event()
    stop = threading.Event()
    writes = []
    clock = [0.0]
    client = _win_accessibility.AccessibilityClient(stop)

    def write(request):
        writes.append(request)
        killed.wait()  # Only termination unblocks this pipe, like a stalled worker.

    def no_reply(*, timeout):
        if stopped:
            stop.set()
        else:
            clock[0] += _win_accessibility._TIMEOUT
        raise queue.Empty

    stream = SimpleNamespace(write=write, flush=lambda: None, close=lambda: None)
    process = SimpleNamespace(
        stdin=stream, stdout=None, poll=lambda: None, kill=killed.set, wait=lambda **_: 0
    )
    monkeypatch.setattr(client, "_start", lambda: process)
    monkeypatch.setattr(client._replies, "get", no_reply)
    monkeypatch.setattr(_win_accessibility.time, "monotonic", lambda: clock[0])
    client._process = process
    with pytest.raises(TargetError) as failed:
        client.call(operation, text="input is never replayed")
    assert killed.is_set() and client._process is None
    assert client._writer is None and len(writes) <= 1
    if stopped:
        assert isinstance(failed.value, InputInterrupted)
    else:
        assert failed.value.code == (
            "computer_use_failed" if operation == "act" else "accessibility_unavailable"
        )
        assert ("effect is unknown" if operation == "act" else "No input was sent") in str(
            failed.value
        )


class Node:
    def __init__(self, identifier, name, *, password=False):
        self.identifier = identifier
        self.CurrentName = name
        self.CurrentIsPassword = password
        self.CurrentBoundingRectangle = SimpleNamespace(left=10, top=20, right=200, bottom=40)
        self.CurrentLocalizedControlType = "edit"
        self.CurrentControlType = 50004
        self.CurrentAutomationId = str(identifier)
        self.CurrentHasKeyboardFocus = True
        self.CurrentIsEnabled = True
        self.CurrentIsOffscreen = False
        self.CurrentIsKeyboardFocusable = True
        self.CurrentIsReadOnly = False
        self.CurrentValue = "value"
        self.parent = None
        self.children = []
        self.values = []

    def GetRuntimeId(self):  # noqa: N802 - native COM interface
        return [1, self.identifier]

    def GetCurrentPropertyValue(self, name):  # noqa: N802 - native COM interface
        assert not self.CurrentIsPassword, "Password patterns must not be read"
        return name == "value"

    def SetValue(self, text):  # noqa: N802 - native COM interface
        self.values.append(text)


def automation():
    root, field, secret = Node(1, "Window"), Node(2, "Subject"), Node(3, "secret", password=True)
    root.children = [field, secret]
    field.parent = secret.parent = root
    instance = object.__new__(_uia_worker.Automation)
    instance.generation = "worker-generation"
    instance.elements = {}
    instance.uia = SimpleNamespace(
        UIA_IsInvokePatternAvailablePropertyId="invoke",
        UIA_IsValuePatternAvailablePropertyId="value",
        UIA_IsTextPatternAvailablePropertyId="text",
    )
    instance.window = lambda request, **_: root
    instance.pattern = lambda element, name: element

    def sibling(element):
        nodes = element.parent.children
        index = nodes.index(element) + 1
        return nodes[index] if index < len(nodes) else None

    instance.walker = SimpleNamespace(
        GetFirstChildElement=lambda element: element.children[0] if element.children else None,
        GetNextSiblingElement=sibling,
        GetParentElement=lambda element: element.parent,
    )
    return instance, root, field, secret


def test_native_inspection_redacts_passwords_and_bounds_the_tree(monkeypatch):
    instance, root, field, secret = automation()
    state = instance.call({"operation": "inspect"})
    assert not state["truncated"]
    assert state["elements"][1]["value"] == "value"
    protected = state["elements"][2]
    assert protected["name"] == "Password field" and protected["actions"] == []
    assert not {"value", "text", "selected_text"} & protected.keys()
    monkeypatch.setattr(_uia_worker, "MAX_ELEMENTS", 2)
    limited = instance.call({"operation": "inspect"})
    assert len(limited["elements"]) == 2 and limited["truncated"]


@pytest.mark.parametrize("change", ["generation", "name", "parent", "disabled"])
def test_native_actions_revalidate_observed_identity_and_capability(change):
    instance, root, field, secret = automation()
    reference = instance.call({"operation": "inspect"})["elements"][1]
    request = {"operation": "act", "reference": reference, "action": "set_value", "text": ""}
    assert instance.call(request) == {}
    assert field.values == [""]
    if change == "generation":
        instance.generation = "new-worker"
    elif change == "name":
        field.CurrentName = "Different field"
    elif change == "parent":
        field.parent = None
    else:
        field.CurrentIsEnabled = False
    with pytest.raises(_uia_worker.RefusedError) as failed:
        instance.call(request)
    assert failed.value.code == (
        "element_not_supported" if change == "disabled" else "element_unavailable"
    )
    assert field.values == [""]
