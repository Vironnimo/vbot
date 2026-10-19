# Windows runtime dependency locks

These files lock the immutable Python 3.14 x86-64 Windows runtimes. Regenerate them from the
repository root with the declared `uv==0.12.11` dependency:

```powershell
python -m uv pip compile pyproject.toml --extra server --extra cli --extra application --python-version 3.14 --python-platform x86_64-pc-windows-msvc --generate-hashes --no-emit-package vbot --output-file scripts/windows/requirements-server.lock
python -m uv pip compile pyproject.toml --extra server --extra cli --extra application --extra desktop --python-version 3.14 --python-platform x86_64-pc-windows-msvc --generate-hashes --no-emit-package vbot --output-file scripts/windows/requirements-server-desktop.lock
python -m uv pip compile pyproject.toml --extra cli --extra application --extra desktop --python-version 3.14 --python-platform x86_64-pc-windows-msvc --generate-hashes --no-emit-package vbot --output-file scripts/windows/requirements-desktop-client.lock
```

Review and commit all lock changes with the corresponding `pyproject.toml` dependency change.
The vBot source is copied separately by the package builder and is deliberately omitted here.
The builder requires wheels for every locked dependency except the Desktop dependency
`proxy-tools==0.1.0`, which is published only as a pure-Python source distribution. Its build
uses a temporary environment provisioned from the wheel-only, hash-checked
`requirements-build.lock`, with pip's automatic build isolation disabled so no unpinned build
dependencies can be fetched. The backend stays outside the packaged runtime. To update it,
choose a setuptools version, verify its wheel digest against its PyPI release metadata and
update `requirements-build.lock`; the prepared runtime cache includes this lock in its key.
Desktop dependency installs disable pip's wheel cache so a wheel built previously with a
different backend cannot bypass this preparation.
Optional local speech dependencies belong to their managed user-data environments, not these
runtime locks.
