"""Choose the existing newline style when writing a text file."""

from __future__ import annotations


def dominant_line_ending(content: str) -> str | None:
    """Return the most frequent CRLF, LF or CR ending, or None when there is none.

    A tie goes to CRLF, then LF. A stray different ending does not determine the
    style of the whole file.
    """
    crlf = content.count("\r\n")
    counts = {
        "\r\n": crlf,
        "\n": content.count("\n") - crlf,
        "\r": content.count("\r") - crlf,
    }
    ending = max(counts, key=counts.__getitem__)
    return ending if counts[ending] else None
