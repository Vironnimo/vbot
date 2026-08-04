#!/usr/bin/env python
"""Add canonical Model-step counts to persisted vBot Run Summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SessionModelStepConversionError(Exception):
    """Raised when Session history cannot be converted safely."""


@dataclass(frozen=True, slots=True)
class SessionConversion:
    """One preflighted Session file that needs summary updates."""

    path: Path
    source_digest: str
    summary_count: int


@dataclass(frozen=True, slots=True)
class SessionModelStepConversionPlan:
    """Complete read-only preflight result for one data directory."""

    data_root: Path
    session_files: int
    current_summaries: int
    conversions: tuple[SessionConversion, ...]

    @property
    def summary_count(self) -> int:
        return sum(conversion.summary_count for conversion in self.conversions)


@dataclass(frozen=True, slots=True)
class SessionModelStepConversionResult:
    """Summary of one applied conversion."""

    converted_files: int
    converted_summaries: int


_SESSION_PATTERNS = (
    "agents/*/sessions/*.jsonl",
    "projects/*/agents/*/sessions/*.jsonl",
    "archive/sessions/agents/*/*.jsonl",
    "archive/sessions/projects/*/agents/*/*.jsonl",
)
_MISSING = object()


def plan_session_model_step_conversion(
    data_dir: str | Path,
) -> SessionModelStepConversionPlan:
    """Preflight every live and archived Session without changing disk."""

    data_root = _resolve_safe_data_root(data_dir)
    conversions: list[SessionConversion] = []
    current_summaries = 0
    session_paths = _session_paths(data_root)
    for path in session_paths:
        raw = _read_regular_session_file(data_root, path)
        converted, missing_count, current_count = _convert_session_bytes(path, raw)
        current_summaries += current_count
        if missing_count == 0:
            continue
        conversions.append(
            SessionConversion(
                path=path,
                source_digest=_digest(raw),
                summary_count=missing_count,
            )
        )
        if converted == raw:
            raise AssertionError("a planned Session conversion must change its bytes")

    return SessionModelStepConversionPlan(
        data_root=data_root,
        session_files=len(session_paths),
        current_summaries=current_summaries,
        conversions=tuple(conversions),
    )


def apply_session_model_step_conversion(
    data_dir: str | Path,
) -> SessionModelStepConversionResult:
    """Apply one fully preflighted, resumable conversion with atomic replaces."""

    plan = plan_session_model_step_conversion(data_dir)
    converted_files = 0
    converted_summaries = 0
    for conversion in plan.conversions:
        raw = _read_regular_session_file(plan.data_root, conversion.path)
        if _digest(raw) != conversion.source_digest:
            raise SessionModelStepConversionError(
                f"Session changed after preflight; stop vBot and retry: {conversion.path}"
            )
        converted, missing_count, _current_count = _convert_session_bytes(conversion.path, raw)
        if missing_count != conversion.summary_count:
            raise SessionModelStepConversionError(
                f"Session conversion plan changed after preflight: {conversion.path}"
            )
        _atomic_replace(conversion.path, converted)
        converted_files += 1
        converted_summaries += missing_count

    return SessionModelStepConversionResult(
        converted_files=converted_files,
        converted_summaries=converted_summaries,
    )


def _resolve_safe_data_root(data_dir: str | Path) -> Path:
    requested = Path(data_dir).expanduser()
    if requested.is_symlink():
        raise SessionModelStepConversionError(
            f"Data directory must not be a symbolic link: {requested}"
        )
    try:
        data_root = requested.resolve(strict=True)
    except FileNotFoundError as error:
        raise SessionModelStepConversionError(
            f"Data directory does not exist: {requested}"
        ) from error
    if not data_root.is_dir():
        raise SessionModelStepConversionError(f"Data directory is not a directory: {data_root}")
    if data_root == Path(data_root.anchor).resolve():
        raise SessionModelStepConversionError(f"Refusing to convert a filesystem root: {data_root}")
    if data_root == Path.home().resolve():
        raise SessionModelStepConversionError(
            f"Refusing to convert the user home directory: {data_root}"
        )
    return data_root


def _session_paths(data_root: Path) -> list[Path]:
    paths = {path for pattern in _SESSION_PATTERNS for path in data_root.glob(pattern)}
    return sorted(paths, key=lambda path: path.as_posix())


def _read_regular_session_file(data_root: Path, path: Path) -> bytes:
    try:
        relative = path.relative_to(data_root)
    except ValueError as error:
        raise SessionModelStepConversionError(
            f"Session path escaped the data directory: {path}"
        ) from error
    current = data_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SessionModelStepConversionError(
                f"Session path must not traverse a symbolic link: {current}"
            )
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise SessionModelStepConversionError(
            f"Cannot inspect Session file {path}: {error}"
        ) from error
    if not stat.S_ISREG(mode):
        raise SessionModelStepConversionError(f"Session path is not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise SessionModelStepConversionError(
            f"Cannot read Session file {path}: {error}"
        ) from error


def _convert_session_bytes(path: Path, raw: bytes) -> tuple[bytes, int, int]:
    if not raw:
        return raw, 0, 0
    if not raw.endswith(b"\n"):
        raise SessionModelStepConversionError(f"Session has an incomplete trailing record: {path}")

    converted_lines: list[bytes] = []
    model_step_count = 0
    missing_summaries = 0
    current_summaries = 0
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        record = _parse_record(path, line_number, line)
        role = record.get("role")
        if role == "assistant":
            model_step_count += 1
        if role != "run_summary":
            converted_lines.append(line)
            continue

        stored_count = record.get("model_step_count", _MISSING)
        if stored_count is _MISSING:
            record["model_step_count"] = model_step_count
            converted_lines.append(_serialize_record(record, line))
            missing_summaries += 1
        else:
            _validate_stored_count(path, line_number, stored_count, model_step_count)
            converted_lines.append(line)
            current_summaries += 1
        model_step_count = 0

    return b"".join(converted_lines), missing_summaries, current_summaries


def _parse_record(path: Path, line_number: int, line: bytes) -> dict[str, Any]:
    try:
        decoded = line.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SessionModelStepConversionError(
            f"Session record is not UTF-8 at {path}:{line_number}"
        ) from error
    try:
        record = json.loads(decoded)
    except json.JSONDecodeError as error:
        raise SessionModelStepConversionError(
            f"Session record is invalid JSON at {path}:{line_number}: {error.msg}"
        ) from error
    if not isinstance(record, dict):
        raise SessionModelStepConversionError(
            f"Session record must be an object at {path}:{line_number}"
        )
    if not isinstance(record.get("role"), str):
        raise SessionModelStepConversionError(
            f"Session record requires a string role at {path}:{line_number}"
        )
    return record


def _validate_stored_count(
    path: Path,
    line_number: int,
    stored_count: object,
    derived_count: int,
) -> None:
    if isinstance(stored_count, bool) or not isinstance(stored_count, int) or stored_count < 0:
        raise SessionModelStepConversionError(f"Invalid model_step_count at {path}:{line_number}")
    if stored_count != derived_count:
        raise SessionModelStepConversionError(
            f"model_step_count mismatch at {path}:{line_number}: "
            f"stored={stored_count} derived={derived_count}"
        )


def _serialize_record(record: dict[str, Any], source_line: bytes) -> bytes:
    line_ending = "\r\n" if source_line.endswith(b"\r\n") else "\n"
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + line_ending).encode(
        "utf-8"
    )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_replace(path: Path, content: bytes) -> None:
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or add canonical Model-step counts to vBot Run Summaries. "
            "Stop the target vBot instance before using --apply."
        )
    )
    parser.add_argument("data_dir", type=Path, help="Existing vBot data directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically rewrite preflighted Session files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = plan_session_model_step_conversion(args.data_dir)
        print(
            "session-model-step-counts..... "
            f"preflight_ok sessions={plan.session_files} "
            f"files={len(plan.conversions)} summaries={plan.summary_count} "
            f"current_summaries={plan.current_summaries} root={plan.data_root}"
        )
        if not args.apply:
            print("session-model-step-counts..... dry-run only; use --apply to rewrite files")
            return 0
        result = apply_session_model_step_conversion(plan.data_root)
    except (SessionModelStepConversionError, OSError) as error:
        print(f"session-model-step-counts..... ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "session-model-step-counts..... "
        f"applied files={result.converted_files} summaries={result.converted_summaries}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
