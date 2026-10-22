# Windows runtime dependency locks

These files lock the immutable Python 3.14 x86-64 Windows runtimes. Regenerate them from the
repository root with the declared `uv==0.12.11` dependency:

```powershell
python scripts/test_dependencies.py refresh
python scripts/test_dependencies.py check
```

The helper regenerates all five runtime locks and three test overlays, retains existing versions where possible, and records the dependency-input fingerprints that CI and push require. It constrains common runtime packages to the Windows server pins and Windows test packages to all three shapes. Review and commit the locks with the corresponding `pyproject.toml` change; bare uv compile commands omit the required fingerprints.
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
