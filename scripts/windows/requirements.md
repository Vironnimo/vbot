# Windows runtime dependency locks

These files lock the immutable Python 3.13 x86-64 Windows runtimes. Regenerate them from the
repository root with the declared `uv==0.12.11` dependency:

```powershell
python -m uv pip compile pyproject.toml --extra server --extra windows-app --python-version 3.13 --python-platform x86_64-pc-windows-msvc --generate-hashes --no-emit-package vbot --output-file scripts/windows/requirements-server.lock
python -m uv pip compile pyproject.toml --extra server --extra windows-app --extra desktop --python-version 3.13 --python-platform x86_64-pc-windows-msvc --generate-hashes --no-emit-package vbot --output-file scripts/windows/requirements-server-desktop.lock
python -m uv pip compile pyproject.toml --extra cli --extra windows-app --extra desktop --python-version 3.13 --python-platform x86_64-pc-windows-msvc --generate-hashes --no-emit-package vbot --output-file scripts/windows/requirements-desktop-client.lock
```

Review and commit all lock changes with the corresponding `pyproject.toml` dependency change.
The vBot source is copied separately by the package builder and is deliberately omitted here.
The builder requires wheels for every locked dependency except `proxy-tools==0.1.0`, which is
published only as a pure-Python source distribution. Pip builds that one package in its default
isolated build environment while retaining the lock's hash verification.
Optional local speech dependencies belong to their managed user-data environments, not these
runtime locks.
