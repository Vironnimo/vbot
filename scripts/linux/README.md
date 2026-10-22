# Linux server package

`scripts/build_linux.py` builds the signed Linux server package
(`vbot-linux-<arch>-server.zip`) for `linux-aarch64` (Raspberry Pi OS 64-bit and other
ARM64 distributions) and `linux-x86_64`. Build each platform on a host of that platform:
the builder runs the runtime it packages to install and check its dependencies.

```bash
python scripts/build_linux.py --source . --output build/linux --platform linux-x86_64 \
  --version "$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")" \
  --revision "$(git rev-parse HEAD)"
python scripts/linux/smoke.py --package build/linux/linux-x86_64/server
```

The WebUI must be built first (`npm ci --prefix webui && npm run build --prefix webui`).

## Files

- `python.lock.json` pins the python-build-standalone CPython of each platform by URL and
  SHA256. The builder removes headers, Tcl/Tk, tests and every link, and keeps one
  interpreter, `runtime/bin/python3`.
- `requirements-server-linux-<arch>.lock` lock the runtime dependencies. All of them install
  from wheels. Regenerate the package locks and fixed test overlays together from the repository
  root with `python scripts/test_dependencies.py refresh` (requires the declared uv version),
  then run `python scripts/test_dependencies.py check`. The helper constrains common packages
  to the Windows server pins and records input fingerprints; bare uv compile commands omit
  those fingerprints and fail CI/push freshness checks.

- `vbot` is the stable bootstrap. The installation keeps a copy at its root, and the
  `vbot` command on the `PATH` links to it. It runs the CLI of the active version, or with
  `--server` that version's server, which is how the systemd user unit `vbot.service`
  starts it.
- `smoke.py` installs a built package into disposable directories and exercises the CLI,
  the server, an update and the uninstall.
