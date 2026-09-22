"""Process output decoding: terminal controls removed, following output kept."""

from __future__ import annotations

import pytest

from core.tools._process_output import ProcessOutputDecoder


def _decode(*chunks: bytes) -> str:
    decoder = ProcessOutputDecoder()
    output = b"".join(decoder.decode(chunk) for chunk in chunks)
    return (output + decoder.decode(b"", final=True)).decode("utf-8")


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((b"\x1b[31mred\x1b[0m plain",), "red plain"),
        ((b"\x1b]0;title\x07after",), "after"),
        ((b"\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\",), "link"),
        ((b"\x1bP1$r0m\x1b\\done",), "done"),
        ((b"\x1b(Bcharset \x1b7saved\x1b8",), "charset saved"),
        (("\x9b1;2Hc1 csi\x9d0;t\x9cx".encode(),), "c1 csix"),
        ((b"\x1b[", b"38;5;", b"12mx"), "x"),
    ],
)
def test_complete_terminal_controls_are_removed(chunks: tuple[bytes, ...], expected: str) -> None:
    assert _decode(*chunks) == expected


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        # Binary-looking junk opened an APC string that never terminates.
        (
            (b"binary\x1b_junk\x00\x01", b"more junk\nERROR: build failed\n"),
            "binary\nERROR: build failed\n",
        ),
        # An aborted CSI must not consume the first character after the line break.
        ((b"\x1b[", b"\nResult: OK"), "\nResult: OK"),
        ((b"\x1b[12\r\nnext",), "\r\nnext"),
        ((b"\x1b\nkept",), "\nkept"),
        (("\x1bä".encode(),), "ä"),
        # A new escape inside an unterminated string starts a new sequence.
        ((b"\x1b]0;title\x1b[31mred\x1b[0m",), "red"),
    ],
)
def test_unterminated_controls_do_not_swallow_following_output(
    chunks: tuple[bytes, ...], expected: str
) -> None:
    assert _decode(*chunks) == expected


def test_control_strings_and_sequences_are_bounded_without_exposing_escape() -> None:
    long_string = b"\x1b]" + b"x" * 5000 + b" visible"
    long_csi = b"\x1b[" + b"1;" * 100 + b"tail"

    string_output = _decode(long_string)
    csi_output = _decode(long_csi)

    assert string_output.endswith(" visible")
    assert len(string_output) < 1000
    assert csi_output.endswith("tail")
    assert "\x1b" not in string_output + csi_output
