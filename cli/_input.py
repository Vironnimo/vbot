"""CLI text and secret input decoding shared by command dispatchers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _prompt_content_from_args(args: argparse.Namespace) -> str:
    content = args.content
    if isinstance(content, str):
        return content
    content_file = args.content_file
    if not isinstance(content_file, str):
        raise ValueError("missing prompt content file")
    return Path(content_file).read_text(encoding="utf-8")


def _optional_content_from_args(args: argparse.Namespace) -> str | None:
    content = getattr(args, "content", None)
    if isinstance(content, str):
        return content
    content_file = getattr(args, "content_file", None)
    if isinstance(content_file, str):
        return Path(content_file).read_text(encoding="utf-8")
    return None


def _read_stdin_utf8() -> str:
    """Read a piped secret or setting value through an explicit UTF-8 contract."""

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read().rstrip("\r\n")
    raw_value = buffer.read()
    if isinstance(raw_value, bytes):
        return raw_value.decode("utf-8-sig").rstrip("\r\n")
    return str(raw_value).rstrip("\r\n")
