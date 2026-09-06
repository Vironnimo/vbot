"""Compact reference entropy, collision claims, and filesystem boundaries."""

import re

import pytest

from core.utils import ids


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


@pytest.mark.parametrize("value", ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"])
def test_file_ids_reject_windows_devices_even_with_a_sidecar_extension(value):
    assert not ids.is_safe_id(value)
