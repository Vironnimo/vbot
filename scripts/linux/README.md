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
  from wheels. Regenerate both from the repository root with the declared `uv==0.12.11`,
  seeding each from the Windows server lock so that every platform runs the same versions:

  ```bash
  for arch in aarch64 x86_64; do
    cp scripts/windows/requirements-server.lock scripts/linux/requirements-server-linux-$arch.lock
    python -m uv pip compile pyproject.toml --extra server --extra application --python-version 3.13 --python-platform $arch-unknown-linux-gnu --generate-hashes --no-emit-package vbot --output-file scripts/linux/requirements-server-linux-$arch.lock
  done
  ```

- `vbot` is the stable bootstrap. The installation keeps a copy at its root, and the
  `vbot` command on the `PATH` links to it. It runs the CLI of the active version, or with
  `--server` that version's server, which is how the systemd user unit `vbot.service`
  starts it.
- `smoke.py` installs a built package into disposable directories and exercises the CLI,
  the server, an update and the uninstall.
