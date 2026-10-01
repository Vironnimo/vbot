"""Exercise a built Linux server package only in disposable installation/data directories."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import zipfile
from pathlib import Path


def _run(command: list[str], environment: dict[str, str], cwd: Path, transcript: list[str]) -> str:
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
    transcript.append(
        f"$ {shlex.join(command)}\nexit code: {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    if result.returncode:
        raise RuntimeError(f"Smoke command failed: {shlex.join(command)}\n{transcript[-1]}")
    return result.stdout


def smoke(package: Path) -> None:
    package = package.resolve()
    versions = list((package / "versions").iterdir())
    if len(versions) != 1:
        raise RuntimeError("Supply a package containing exactly one built version")
    payload = versions[0]
    manifest = json.loads((payload / "release.json").read_text(encoding="utf-8"))
    temporary = Path(tempfile.mkdtemp(prefix="vbot-linux-smoke-"))
    root, data, home = temporary / "application", temporary / "data", temporary / "home"
    home.mkdir()
    # A disposable home keeps the user's systemd units and command links untouched.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cli = root / "vbot"
    transcript: list[str] = []
    completed = False
    try:
        _run(
            [
                str(payload / "runtime" / "bin" / "python3"),
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "cli.application.install",
                "--root",
                str(root),
                "--payload",
                str(payload),
                "--shape",
                "server",
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
        # The command on the PATH is a link to the bootstrap at the installation root.
        link = home / ".local" / "bin" / "vbot"
        link.parent.mkdir(parents=True)
        link.symlink_to(cli)
        installed = json.loads(
            _run([str(link), "application", "status"], environment, temporary, transcript)
        )
        if installed["shape"] != "server" or installed["version"] != manifest["version_id"]:
            raise RuntimeError(f"The installation reports the wrong version: {installed}")
        initial = root / "versions" / installed["version"]
        _run([str(cli), "server", "start"], environment, temporary, transcript)
        _run([str(cli), "server", "status"], environment, temporary, transcript)
        # A distinct local identity exercises the complete independent updater:
        # snapshot, verification start and the normal restart.
        candidate_id = "smoke_" + installed["version"][:110]
        archive = temporary / "candidate.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for path in payload.rglob("*"):
                if path.is_file() and path != payload / "release.json":
                    bundle.write(path, path.relative_to(payload).as_posix())
            bundle.writestr("release.json", json.dumps(dict(manifest, version_id=candidate_id)))
        _run([str(cli), "update", "--package", str(archive)], environment, temporary, transcript)
        outcome = json.loads(
            _run([str(cli), "update", "status"], environment, temporary, transcript)
        )
        active = (root / "active-version").read_text(encoding="ascii").strip()
        if outcome["phase"] != "completed" or active != candidate_id:
            raise RuntimeError(f"The update did not verify its candidate: {outcome}")
        _run([str(cli), "server", "status"], environment, temporary, transcript)
        _run([str(cli), "server", "stop"], environment, temporary, transcript)
        _run(
            [
                str(root / "versions" / candidate_id / "runtime" / "bin" / "python3"),
                "-I",
                "-B",
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
        _run([str(link), "uninstall", "--app-only", "--yes"], environment, temporary, transcript)
        if root.exists() or link.is_symlink() or not (data / "settings.json").is_file():
            raise RuntimeError("Uninstall did not remove exactly the application")
        completed = True
        print("Linux server install, CLI, update, lifecycle and uninstall verified")
    finally:
        if not completed:
            if cli.exists():
                try:
                    _run([str(cli), "server", "stop"], environment, temporary, transcript)
                except Exception as error:
                    print(f"Disposable server cleanup needs attention: {error}")
            print("\n\n".join(transcript))
            # Update and startup failures name these logs; the runner is gone afterwards.
            for log in sorted((root / "logs").glob("*.log")):
                print(f"\n--- {log}\n{log.read_text(encoding='utf-8', errors='replace')}")
            print(f"Smoke evidence retained at {temporary}")
        else:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    smoke(parser.parse_args().package)
