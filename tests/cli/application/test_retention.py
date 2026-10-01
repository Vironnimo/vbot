"""Version cleanup tests use temporary installations and injected process tables."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import psutil  # type: ignore[import-untyped]
import pytest

from cli.application import retention
from cli.application.state import Installation, Operation


def _install(root: Path, versions: list[str], *, active: str) -> Installation:
    install = Installation(root, "server", "127.0.0.1", 8420, str((root / "data").resolve()))
    for version_id in versions:
        version = root / "versions" / version_id
        (version / "runtime").mkdir(parents=True)
        (version / "release.json").write_text("{}", encoding="utf-8")
        (version / "runtime" / "python314.dll").write_bytes(b"dll")
    (root / "active-version").write_text(f"{active}\n", encoding="ascii")
    return install


def _operation(
    install: Installation,
    operation_id: str,
    phase: str,
    previous: str,
    candidate: str | None,
    *,
    at: int,
) -> None:
    operation = Operation(
        id=operation_id,
        phase=phase,
        previous_version=previous,
        candidate_version=candidate,
        created_at=f"2026-09-{at:02d}T12:00:00+00:00",
    )
    operation.save(install)


def _environment(directory: Path, home: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "pyvenv.cfg").write_text(
        f"home = {home}\nimplementation = CPython\n", encoding="utf-8"
    )


class _Process:
    def __init__(self, pid: int, exe: Path | None, *, name: str = "", maps=()) -> None:
        self.pid = pid
        self.info = {"name": name or (exe.name if exe else ""), "exe": str(exe) if exe else None}
        self._maps = maps

    def memory_maps(self, grouped: bool = True):
        if isinstance(self._maps, Exception):
            raise self._maps
        return [SimpleNamespace(path=str(path)) for path in self._maps]


def _processes(monkeypatch: pytest.MonkeyPatch, *processes: _Process) -> None:
    monkeypatch.setattr(retention.psutil, "process_iter", lambda _attributes: iter(processes))


def _other_case(name: str) -> str:
    """Windows names the same directory in any letter case."""
    return name.upper() if os.name == "nt" else name


def _versions(install: Installation) -> set[str]:
    return {path.name for path in (install.root / "versions").iterdir()}


def test_cleanup_retires_only_versions_nothing_can_still_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vBot"
    kept = {
        "rel_active",
        "rel_fallback",
        "rel_failed_since",
        "rel_prepared_since",
        "rel_unfinished",
        "rel_speech_env",
        "rel_embedding_env",
        "rel_tray",
        "rel_server",
    }
    install = _install(root, [*kept, "rel_older", "rel_unused"], active="rel_active")
    _operation(install, "upd_older", "completed", "rel_older", "rel_fallback", at=1)
    _operation(install, "upd_unfinished", "preparing", "rel_active", "rel_unfinished", at=2)
    _operation(install, "upd_activated", "completed", "rel_fallback", "rel_active", at=3)
    _operation(install, "upd_failed", "rolled_back", "rel_active", "rel_failed_since", at=4)
    _operation(install, "upd_prepared", "prepared", "rel_active", "rel_prepared_since", at=5)
    _operation(install, "upd_current", "completed", "rel_active", "rel_active", at=6)
    versions = root / "versions"
    _environment(
        root / "data" / "speech-engines" / "stt",
        versions / _other_case("rel_speech_env") / "runtime",
    )
    _environment(root / "data" / "speech-engines" / "tts", tmp_path / "uv" / "cpython")
    _environment(
        root / "data" / "embedding-engines" / "onnx", versions / "rel_embedding_env" / "runtime"
    )
    # The worker itself runs from the fallback version; the tray host is a root
    # bootstrap that loaded an older version; unrelated processes are ignored.
    _processes(
        monkeypatch,
        _Process(10, versions / "rel_fallback" / "runtime" / "vBot.Update.exe"),
        _Process(11, versions / _other_case("rel_server") / "runtime" / "vBot.Server.exe"),
        _Process(12, root / "vBot.exe", maps=[versions / "rel_tray" / "runtime" / "python314.dll"]),
        _Process(13, tmp_path / "elsewhere" / "python.exe", maps=psutil.AccessDenied()),
        _Process(14, None, name="System"),
    )
    (versions / "rel_incomplete").mkdir()
    (versions / "not a version").mkdir()

    retired = retention.retire_unneeded(install)

    assert _versions(install) == kept | {"rel_incomplete", "not a version"}
    assert sorted(path.name for path in retired) == [
        "retired-versions-rel_older",
        "retired-versions-rel_unused",
    ]
    assert all(path.parent == root / "staging" for path in retired)

    retention.remove_retired(retired)

    assert not any(path.exists() for path in retired)
    assert _versions(install) == kept | {"rel_incomplete", "not a version"}


@pytest.mark.parametrize(
    "process",
    [
        _Process(20, Path("C:/placeholder/vBot.exe"), maps=psutil.AccessDenied()),
        _Process(21, None, name="vBot.Server.exe"),
    ],
    ids=["unreadable-modules", "unreadable-executable"],
)
def test_an_uninspectable_vbot_process_keeps_every_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, process: _Process
) -> None:
    install = _install(tmp_path, ["rel_active", "rel_unused"], active="rel_active")
    if process.info["exe"] is not None:
        process.info["exe"] = str(tmp_path / "vBot.exe")
    _processes(monkeypatch, process)

    assert retention.retire_unneeded(install) == []
    assert _versions(install) == {"rel_active", "rel_unused"}


def test_an_unreadable_record_keeps_every_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install = _install(tmp_path, ["rel_active", "rel_unused"], active="rel_active")
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "upd_broken.json").write_text("{", encoding="utf-8")
    _processes(monkeypatch)

    with caplog.at_level(logging.INFO, logger="vbot.application.retention"):
        assert retention.retire_unneeded(install) == []

    assert _versions(install) == {"rel_active", "rel_unused"}
    # Keeping every version is a failed cleanup, not routine progress.
    assert [
        record.levelno for record in caplog.records if record.name == "vbot.application.retention"
    ] == [logging.WARNING]


def test_downloads_of_finished_operations_are_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path, ["rel_active"], active="rel_active")
    _operation(install, "upd_done", "failed", "rel_active", None, at=1)
    _operation(install, "upd_running", "preparing", "rel_active", None, at=2)
    for operation_id in ("upd_done", "upd_running", "upd_unrecorded"):
        (tmp_path / "downloads" / operation_id).mkdir(parents=True)
    _processes(monkeypatch)

    retired = retention.retire_unneeded(install)

    assert sorted(path.name for path in retired) == [
        "retired-downloads-upd_done",
        "retired-downloads-upd_unrecorded",
    ]
    assert [path.name for path in (tmp_path / "downloads").iterdir()] == ["upd_running"]


def test_a_version_that_cannot_be_moved_is_kept_for_a_later_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path, ["rel_active", "rel_open", "rel_unused"], active="rel_active")
    _processes(monkeypatch)
    rename = Path.rename

    def refuse_open_tree(path: Path, target: Path) -> Path:
        if path.name == "rel_open":
            raise PermissionError("in use")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", refuse_open_tree)

    retired = retention.retire_unneeded(install)

    assert [path.name for path in retired] == ["retired-versions-rel_unused"]
    assert _versions(install) == {"rel_active", "rel_open"}


def test_trees_left_by_an_interrupted_cleanup_are_removed_even_when_versions_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _install(tmp_path, ["rel_active", "rel_unused"], active="rel_active")
    leftover = tmp_path / "staging" / "retired-versions-rel_gone"
    (leftover / "runtime").mkdir(parents=True)
    unrelated = tmp_path / "staging" / "release-extracting"
    unrelated.mkdir()
    _processes(monkeypatch, _Process(30, None, name="vBot.exe"))

    retired = retention.retire_unneeded(install)
    retention.remove_retired(retired)

    assert retired == [leftover]
    assert not leftover.exists()
    assert unrelated.is_dir()
    assert _versions(install) == {"rel_active", "rel_unused"}
