"""CLI text and secret input decoding shared by command dispatchers."""

from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path


def _read_text_file(path: str) -> str:
    """Read shell-authored content without leaking a BOM into the saved text."""

    raw = Path(path).read_bytes()
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError("UTF-32 content is unsupported; save the file as UTF-8 or UTF-16 with BOM")
    encoding = (
        "utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    )
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        label = "UTF-16" if encoding == "utf-16" else "UTF-8"
        raise ValueError(
            f"Cannot decode the content file as {label}; save it as UTF-8 or UTF-16 with BOM"
        ) from None
    # Match text-mode universal newlines on every supported platform.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _prompt_content_from_args(args: argparse.Namespace) -> str:
    content = args.content
    if isinstance(content, str):
        return content
    content_file = args.content_file
    if not isinstance(content_file, str):
        raise ValueError("missing prompt content file")
    return _read_text_file(content_file)


def _optional_content_from_args(args: argparse.Namespace) -> str | None:
    content = getattr(args, "content", None)
    if isinstance(content, str):
        return content
    content_file = getattr(args, "content_file", None)
    if isinstance(content_file, str):
        return _read_text_file(content_file)
    return None


def _read_secret(prompt: str) -> str:
    """Read a secret from a hidden terminal prompt, or from stdin when it is piped.

    Never from the command line, where shell history and process lists keep it.
    """

    stdin = sys.stdin
    if stdin is not None and stdin.isatty():
        import getpass

        return getpass.getpass(prompt)
    return _read_stdin_utf8()


def _read_stdin_utf8() -> str:
    """Read a piped secret or setting value through an explicit UTF-8 contract."""

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read().rstrip("\r\n")
    raw_value = buffer.read()
    if isinstance(raw_value, bytes):
        return raw_value.decode("utf-8-sig").rstrip("\r\n")
    return str(raw_value).rstrip("\r\n")
