"""Strip ANSI escape sequences from captured subprocess output.

Terminal programs emit ANSI/VT control sequences (colors, cursor moves,
title-setting OSC strings) that are meaningless once their output is captured as
text. Two concrete harms make stripping them at the process-output boundary
worth it:

- A model that sees escape sequences in tool output tends to copy them verbatim
  into later file writes, silently corrupting source and config files.
- The sequences are pure token noise in the model's context and render as
  garbage in the plain-text output view.

The pattern covers the ECMA-48 spec: CSI (including the private-mode ``?``
prefix, colon-separated params, and intermediate bytes), OSC (BEL and ST
terminators), DCS/SOS/PM/APC string sequences, nF multi-byte escapes, Fp/Fe/Fs
single-byte escapes, and 8-bit C1 control characters.
"""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"  # CSI sequence
    r"|\][\s\S]*?(?:\x07|\x1b\\)"  # OSC (BEL or ST terminator)
    r"|[PX^_][\s\S]*?(?:\x1b\\)"  # DCS/SOS/PM/APC strings
    r"|[\x20-\x2f]+[\x30-\x7e]"  # nF escape sequences
    r"|[\x30-\x7e]"  # Fp/Fe/Fs single-byte
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"  # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"  # 8-bit OSC
    r"|[\x80-\x9f]",  # other 8-bit C1 controls
    re.DOTALL,
)

# Fast-path probe: skip the full regex when no escape-introducing byte is
# present, so clean text passes through with negligible overhead.
_HAS_ESCAPE_RE = re.compile(r"[\x1b\x80-\x9f]")


def strip_ansi(text: str) -> str:
    """Return ``text`` with ANSI escape sequences removed.

    Returns the input unchanged (fast path) when it contains no ESC or 8-bit C1
    bytes. Safe to call on any string — clean text is returned as-is.
    """
    if not text or not _HAS_ESCAPE_RE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


__all__ = ["strip_ansi"]
