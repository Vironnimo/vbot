"""Compact reference entropy, collision claims, and filesystem boundaries."""

import re

import pytest

from core.utils import ids


def test_id_entry_lookup_requires_the_exact_stored_spelling(tmp_path):
    (tmp_path / "vbot").mkdir()

    assert ids.has_id_entry(tmp_path, "vbot") is True
    # A case-insensitive filesystem opens ``VBOT`` for ``vbot``; ids stay exact.
    assert ids.has_id_entry(tmp_path, "VBOT") is False
    assert ids.has_id_entry(tmp_path, "missing") is False
    assert ids.has_id_entry(tmp_path / "absent", "vbot") is False


def test_claimed_id_uses_all_sixty_bits_and_retries_only_collisions(monkeypatch):
    values = iter((0, (1 << 60) - 1))
    widths = []
    claims = []

    def bits(width):
        widths.append(width)
        return next(values)

    def claim(candidate):
        claims.append(candidate)
        return candidate != "sub_000000000000"

    monkeypatch.setattr(ids.secrets, "randbits", bits)
    assert ids.new_id("sub", claim=claim) == "sub_zzzzzzzzzzzz"
    assert claims == ["sub_000000000000", "sub_zzzzzzzzzzzz"]
    assert widths == [60, 60]


def test_claim_failure_propagates_without_repeating_side_effects():
    calls = []

    def claim(candidate):
        calls.append(candidate)
        raise PermissionError

    with pytest.raises(PermissionError):
        ids.new_id("att", claim=claim)
    assert len(calls) == 1


def test_unclaimed_message_ids_keep_eighty_bits(monkeypatch):
    from core.chat import ChatMessage

    widths = []

    def bits(width):
        widths.append(width)
        return (1 << width) - 1

    monkeypatch.setattr(ids.secrets, "randbits", bits)
    message = ChatMessage.user("test")
    assert message.id == "msg_zzzzzzzzzzzzzzzz"
    assert ChatMessage.from_dict(message.to_dict()).id == message.id
    assert widths == [80]


def test_generated_ids_are_lowercase_path_safe_and_type_distinguishable():
    prefixes = ("sub", "ses", "proc", "term", "cron", "evt", "act", "att", "img", "aud")
    values = [ids.new_id(prefix, claim=lambda _: True) for prefix in prefixes]
    assert len(set(values)) == len(values)
    for prefix, value in zip(prefixes, values, strict=True):
        assert re.fullmatch(prefix + r"_[0-9a-hjkmnp-tv-z]{12}", value)
        assert ids.is_safe_id(value)


@pytest.mark.parametrize(
    "value", [None, 12, "", "../a", "a/b", "a\\b", "a:stream", "a.", "a\n", "a" * 129]
)
def test_opaque_file_ids_reject_unsafe_paths(value):
    assert not ids.is_safe_id(value)


@pytest.mark.parametrize(
    "value", ["att_0123456789ab", "00000000-0000-4000-8000-000000000001", "a" * 32]
)
def test_opaque_file_ids_do_not_require_a_generation_format(value):
    assert ids.is_safe_id(value)


@pytest.mark.parametrize(
    "value", ["con", "prn", "aux", "nul", "com0", "com1", "com9", "lpt0", "lpt1", "lpt9"]
)
def test_file_ids_reject_windows_devices_even_with_a_sidecar_extension(value):
    assert not ids.is_safe_id(value)


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("con", True),
        ("Aux.json", True),
        ("nul .txt", True),
        ("COM0", True),
        ("lpt\u00b9.log", True),
        ("CONIN$", True),
        ("a:b", True),
        ("a/b", True),
        ("a\\b", True),
        ("name.", True),
        ("name ", True),
        ("a|b", True),
        ("a\x01", True),
        ("console", False),
        ("con-default.json", False),
        ("com10", False),
        (".", False),
        ("..", False),
    ],
)
def test_reserved_names_are_the_names_windows_cannot_store(name, reserved):
    assert ids.is_reserved_name(name) is reserved


def test_reserved_name_message_names_the_reason_and_the_way_out():
    device = ids.reserved_name_message("Agent id", "aux.json")
    assert device.startswith("The Agent id 'aux.json' is reserved on Windows: AUX is a device")
    assert device.endswith("choose a different Agent id.")
    assert "cannot contain" in ids.reserved_name_message("file or folder name", "a:b")
