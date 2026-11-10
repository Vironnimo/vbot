"""UIA contracts with in-memory providers and pipes; no real desktop or subprocesses."""

from __future__ import annotations

import io
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import _uia_worker, _win_accessibility
from resources.extensions.computer_use.target import InputInterrupted, TargetError


@pytest.mark.parametrize("host", ["python.exe", "vBot.Server.exe", "vBot.Desktop.exe"])
def test_uia_launch_uses_the_generic_host_in_packaged_apps(monkeypatch, tmp_path, host):
    calls = []
    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        poll=lambda: 0,
        wait=lambda **_: 0,
    )
    monkeypatch.setattr(
        _win_accessibility,
        "sys",
        SimpleNamespace(platform="win32", executable=str(tmp_path / host)),
    )

    def launch(arguments, **options):
        calls.append((arguments, options))
        return process

    monkeypatch.setattr(_win_accessibility.subprocess, "Popen", launch)
    client = _win_accessibility.AccessibilityClient(threading.Event())
    try:
        client._start()
    finally:
        client.close()
    arguments, options = calls[0]
    expected = "python.exe" if host == "python.exe" else "vBot.Python.exe"
    assert Path(arguments[0]) == tmp_path / expected
    assert Path(arguments[-1]).is_absolute() and Path(arguments[-1]).name == "_uia_worker.py"
    assert "-B" in arguments and "-E" in arguments and "-P" in arguments
    if sys.platform == "win32":
        assert options["creationflags"] & _win_accessibility.subprocess.CREATE_NO_WINDOW


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
        self.text = None
        self.selections = []

    def GetRuntimeId(self):  # noqa: N802 - native COM interface
        return [1, self.identifier]

    def GetCurrentPropertyValue(self, name):  # noqa: N802 - native COM interface
        assert not self.CurrentIsPassword, "Password patterns must not be read"
        return name == "value" or (name == "text" and self.text is not None)

    @property
    def DocumentRange(self):  # noqa: N802 - native COM interface
        return SimpleNamespace(GetText=lambda limit: self.range_text(self.text, limit))

    def GetSelection(self):  # noqa: N802 - native COM interface
        return SimpleNamespace(
            Length=len(self.selections),
            GetElement=lambda index: SimpleNamespace(
                GetText=lambda limit: self.range_text(self.selections[index], limit)
            ),
        )

    @staticmethod
    def range_text(value, limit):
        return value.encode("utf-16-le")[: 2 * limit].decode("utf-16-le", errors="surrogatepass")

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
        UIA_EditControlTypeId=50004,
        UIA_DocumentControlTypeId=50030,
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


@pytest.mark.parametrize("character", ["x", "\U0001f600"])
@pytest.mark.parametrize(
    "key,length,selections,shortened",
    [
        ("value", 4096, 0, False),
        ("value", 4097, 0, True),
        ("text", 4096, 0, False),
        ("text", 4097, 0, True),
        ("selected_text", 4096, 1, False),
        ("selected_text", 4097, 1, True),
        ("selected_text", 2048, 2, True),
        ("selected_text", 1, 4, False),
        ("selected_text", 1, 5, True),
    ],
)
def test_native_inspection_marks_incomplete_field_content(
    key, length, selections, shortened, character
):
    instance, root, field, secret = automation()
    value = character * length
    if key == "value":
        field.CurrentValue = value
    elif key == "text":
        field.text = value
    else:
        field.text = ""
        field.selections = [value] * selections
    state = instance.call({"operation": "inspect"})
    item = state["elements"][1]
    expected = "\n".join([value] * min(selections, 4)) if key == "selected_text" else value
    assert item[key] == expected[: _uia_worker.MAX_TEXT]
    assert item.get("truncated_fields", []) == ([key] if shortened else [])
    assert state["truncated"] is shortened


@pytest.mark.parametrize("control_type", [50004, 50030])
@pytest.mark.parametrize(
    "full_value,paragraph_end",
    [
        pytest.param("x" * 2500, "", id="complete-ascii"),
        pytest.param("x" * 5000, "", id="provider-and-client-cut-ascii"),
        pytest.param("\U0001f600" * 2500, "", id="provider-cut-emoji"),
        pytest.param("\U0001f600" * 5000, "", id="provider-and-client-cut-emoji"),
        pytest.param("x" * 2500, "\r\n", id="complete-with-paragraph-end"),
        pytest.param("x" * 4095 + "\U0001f600suffix", "", id="provider-split-surrogate"),
    ],
)
def test_native_inspection_detects_value_truncation_inside_edit_providers(
    full_value, paragraph_end, control_type
):
    instance, root, field, secret = automation()
    field.CurrentValue = field.range_text(full_value, 4096)
    field.CurrentControlType = control_type
    field.text = full_value + paragraph_end
    state = instance.call({"operation": "inspect"})
    item = state["elements"][1]
    assert item["value"] == field.CurrentValue.rstrip("\ud83d")
    item["value"].encode("utf-8")  # No provider-split surrogate reaches the Model.
    expected_fields = []
    if field.CurrentValue != full_value:
        expected_fields.append("value")
    if len(field.text) > _uia_worker.MAX_TEXT:
        expected_fields.append("text")
    assert item["text"] == field.text[: _uia_worker.MAX_TEXT]
    assert item.get("truncated_fields", []) == expected_fields
    assert state["truncated"] is bool(expected_fields)


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
