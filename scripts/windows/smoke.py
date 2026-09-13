"""Exercise a built native payload only in disposable installation/data directories."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


def _output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _run(command: list[str], environment: dict[str, str], cwd: Path, transcript: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        transcript.append(
            "\n".join(
                (
                    f"$ {subprocess.list2cmdline(command)}",
                    "timed out after 300 seconds",
                    "stdout:",
                    _output_text(error.stdout),
                    "stderr:",
                    _output_text(error.stderr),
                )
            )
        )
        raise
    transcript.append(
        "\n".join(
            (
                f"$ {subprocess.list2cmdline(command)}",
                f"exit code: {result.returncode}",
                "stdout:",
                result.stdout,
                "stderr:",
                result.stderr,
            )
        )
    )
    if result.returncode:
        raise RuntimeError(f"Native smoke command failed: {result.stdout}\n{result.stderr}")
    return result.stdout


def _contained_detached_update(
    cli: Path,
    python: Path,
    archive: Path,
    environment: dict[str, str],
    cwd: Path,
    transcript: list[str],
) -> dict[str, object]:
    parent_code = (
        "import subprocess,sys; "
        "from core.utils.processes import activate_process_containment; "
        "activate_process_containment(); "
        "result=subprocess.run([sys.argv[1],'update','--package',sys.argv[2],'--detach']); "
        "raise SystemExit(result.returncode)"
    )
    _run(
        [str(python), "-c", parent_code, str(cli), str(archive)],
        environment,
        cwd,
        transcript,
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        outcome_value: object = json.loads(
            _run([str(cli), "update", "status"], environment, cwd, transcript)
        )
        if not isinstance(outcome_value, dict):
            raise RuntimeError("Update status did not return an object")
        outcome = outcome_value
        if outcome.get("complete"):
            return outcome
        time.sleep(0.25)
    raise RuntimeError("Detached update did not reach a terminal state")


def _copy_evidence_entry(source: Path, destination: Path) -> None:
    if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
        return
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    elif source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_evidence_entry(child, destination / child.name)


def _write_failure_evidence(temporary: Path, evidence: Path, transcript: list[str]) -> None:
    root, data = temporary / "application", temporary / "data"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "command-output.txt").write_text("\n\n".join(transcript) + "\n", encoding="utf-8")
    for source, relative in (
        (root / "application.json", "application.json"),
        (root / "active-version", "active-version"),
        (root / "host.json", "host.json"),
        (root / "removal-pending.json", "removal-pending.json"),
        (root / "operations", "operations"),
        (root / "logs", "application-logs"),
        (data / "settings.json", "data/settings.json"),
        (data / "session-store.json", "data/session-store.json"),
        (data / "session-recovery.json", "data/session-recovery.json"),
        (data / "logs", "data/logs"),
    ):
        _copy_evidence_entry(source, evidence / relative)


def smoke(package: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Native Windows payload smoke requires Windows")
    package = package.resolve()
    versions = list((package / "versions").iterdir())
    if len(versions) != 1:
        raise RuntimeError("Supply a package containing exactly one built version")
    payload = versions[0]
    manifest = json.loads((payload / "release.json").read_text(encoding="utf-8"))
    shape = manifest["install_shape"]
    evidence = package.parent.parent / "smoke-evidence" / shape
    temporary = Path(tempfile.mkdtemp(prefix="vbot-native-smoke-"))
    root, data = temporary / "application", temporary / "data"
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONUTF8="1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cli = root / "vBot.exe"
    completed = False
    tray: subprocess.Popen[bytes] | None = None
    transcript: list[str] = []
    try:
        _run(
            [
                str(package / "vBot.exe"),
                "application",
                "install",
                "--root",
                str(root),
                "--payload",
                str(payload),
                "--shape",
                shape,
                "--data-dir",
                str(data),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            environment,
            temporary,
            transcript,
        )
        environment["VBOT_INSTALL_ROOT"] = str(root)
        installed = json.loads(
            _run([str(cli), "application", "status"], environment, temporary, transcript)
        )
        if installed["shape"] != shape or (root / ".git").exists():
            raise RuntimeError("Native installation has the wrong shape or includes a checkout")
        initial = root / "versions" / installed["version"]
        python = initial / "runtime" / "vBot.Python.exe"
        _run(
            [
                str(python),
                "-c",
                "import sys; assert sys.flags.isolated and sys.dont_write_bytecode",
            ],
            environment,
            temporary,
            transcript,
        )
        if shape != "server":
            _run(
                [str(python), "-c", "import desktop.main, webview, pythoncom, win32api"],
                environment,
                temporary,
                transcript,
            )
        if shape != "desktop-client":
            _run([str(cli), "server", "start"], environment, temporary, transcript)
            _run([str(cli), "server", "status"], environment, temporary, transcript)
        # A distinct local identity exercises the complete independent updater,
        # including snapshot/verification/normal restart for server shapes.
        candidate_id = "smoke_" + installed["version"][:110]
        archive = temporary / "candidate.zip"
        updated = dict(manifest, version_id=candidate_id)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for path in payload.rglob("*"):
                if path.is_file() and path != payload / "release.json":
                    bundle.write(path, path.relative_to(payload).as_posix())
            bundle.writestr("release.json", json.dumps(updated))
        if shape == "desktop-client":
            _run(
                [str(cli), "update", "--package", str(archive)],
                environment,
                temporary,
                transcript,
            )
            outcome = json.loads(
                _run([str(cli), "update", "status"], environment, temporary, transcript)
            )
        else:
            outcome = _contained_detached_update(
                cli, python, archive, environment, temporary, transcript
            )
        active = (root / "active-version").read_text(encoding="ascii").strip()
        if outcome["phase"] != "completed" or active != candidate_id:
            raise RuntimeError(f"Native update did not verify its candidate: {outcome}")
        if shape != "desktop-client":
            _run([str(cli), "server", "stop"], environment, temporary, transcript)
        tray = subprocess.Popen(
            [str(cli)],
            cwd=temporary,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 90
        while not (root / "host.json").is_file():
            if tray.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("The native tray host did not initialize")
            time.sleep(0.1)
        _run([str(cli), "application", "exit"], environment, temporary, transcript)
        if tray.wait(timeout=30) != 0:
            raise RuntimeError("The native tray host did not exit cleanly")
        _run(
            [
                str(python),
                "-c",
                "from cli.application.packages import validate_release; "
                "from pathlib import Path; import sys; "
                "[validate_release(Path(p)) for p in sys.argv[1:]]",
                str(initial),
                str(root / "versions" / candidate_id),
            ],
            environment,
            temporary,
            transcript,
        )
        completed = True
        print(f"Native {shape} install, update, tray, lifecycle and immutable inventory verified")
    finally:
        if tray is not None and tray.poll() is None:
            tray.terminate()
            tray.wait(timeout=10)
        if not completed and cli.exists() and shape != "desktop-client":
            try:
                _run([str(cli), "server", "stop"], environment, temporary, transcript)
            except Exception as error:
                print(f"Disposable server cleanup needs attention: {error}")
        if completed:
            shutil.rmtree(temporary)
        else:
            try:
                _write_failure_evidence(temporary, evidence, transcript)
            except OSError as error:
                print(f"Native smoke failure evidence could not be copied: {error}")
            else:
                print(f"Native smoke failure evidence copied to {evidence}")
            print(f"Native smoke evidence retained at {temporary}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    smoke(parser.parse_args().package)
