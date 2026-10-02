# Native Windows application packages

`scripts/build_windows.py` builds three x86-64 application shapes: `server`,
`server-desktop`, and `desktop-client`. The application uses readable Python
sources and a private CPython 3.14 runtime. Native hosts load CPython in-process;
the stable root `vBot.exe` starts the tray with no arguments and the CLI with
arguments. Its stable `vBot.GUI.exe` companion uses the Windows GUI subsystem;
the Desktop shortcut invokes it with `desktop` and follows `active-version`.
Installation places both stable executables; updates never replace them.
Desktop is independently launched and does not own server lifetime.
No Windows service is installed.

## Build inputs and outputs

Use a clean checkout, a full CPython 3.14 x86-64 runtime with `venv` and
`ensurepip`, LLVM's `clang-cl` and `llvm-rc` with the Windows SDK/linker libraries,
Node.js/npm for server WebUI assets, and Inno Setup 6 for the installer. The
[Windows package workflow](../../.github/workflows/windows-package.yml) records
the CI build sequence for all shapes. Runtime requirements are pinned and hashed
per shape; see [requirements.md](requirements.md) for regeneration. All shapes
bundle vBot's one supported Python version (`scripts/package_build.PYTHON_VERSION`,
see `.vorch/PROJECT.md` -> Development -> Python version); the builder rejects a
runtime from another Python minor version.

For example, from the repository root after building the WebUI:

```powershell
$revision = git rev-parse HEAD
python scripts/build_windows.py --source . --runtime C:\Python314 --output build/windows --shape server --version 0.1.0 --revision $revision
```

The runtime argument is an input directory: the builder copies it and provisions
the copied runtime's dependency directory from the selected lock file. It does
not install vBot or its packages into the supplied interpreter. The version
argument must be the version being built, not the example value above.
Use a short build output path for Inno compilation: deeply nested build roots can
exceed the compiler's file path limit even when the native payload itself works.

Exercise a built payload in disposable installation and data directories, adding
`--speech` for the server shape to provision the managed speech recipes:

```powershell
python scripts/windows/smoke.py --package build/windows/windows-x86_64/server
```

The workflow file shows the `ISCC.exe` definitions that compile
[vbot.iss](vbot.iss) into the installer. `smoke_installer.ps1` installs and
uninstalls that installer for real and therefore runs only in a disposable CI
runner.

The copied runtime's `DLLs/sqlite3.dll` is replaced with the official SQLite
build pinned in [sqlite.lock.json](sqlite.lock.json), downloaded from sqlite.org
at build time and verified against both recorded digests. CPython's bundled
SQLite would confine Sessions to the slower rollback journal. To move the pin,
record the new archive URL, its SHA3-256 (published on the sqlite.org download
page) and the SHA256 of the contained `sqlite3.dll`.

Outputs include:

- `windows-x86_64/<shape>/`: stable bootstrap plus `versions/<id>/app`,
  `runtime`, and the complete hashed `release.json` inventory.
- `artifacts/vbot-windows-x86_64-<shape>.zip`: the version payload for updates.
- `artifacts/vbot-windows-x86_64-<shape>-runtime-inventory.json`: dependency names
  and exact versions for inspection.
- `artifacts/vbot-release.json`: the version identity an updater compares with its
  active version before downloading. The publishing workflows add the SHA-256
  digest of every other release asset to it (`scripts/release_assets.py
  --record-digests`).
- `artifacts/release-public-key.txt`, and a sibling `.zip.sig` for signed builds.
- After Inno compilation, `installers/vBot-<version>-windows-x86_64-<shape>.exe`.

Application collection excludes Git metadata, repository tests, development
environments and build tooling. It includes the required runtime sources,
resources, built WebUI/Extension assets, license and notices. Native bootstrap
protocol 1 is fixed across normal updates; incompatible protocols require an
installer upgrade instead of replacing a live bootstrap.

## Signing and publication

`--release-mode` requires an exact clean source revision and
`VBOT_RELEASE_SIGNING_KEY`, containing a base64 raw Ed25519 private key. The
builder signs the raw SHA256 digest of each archive and emits its public key for
the installer. Keep this private key in repository secrets. The installer stores
only the public key; official updates fail closed without it. `--channel release|main`
and the installer's `UpdateChannel` definition record the update channel the
installed package follows. A development build without a release key is usable
with an explicitly selected local `vbot update --package <archive>`.

CI builds and signs every published package; nothing is published from a local
build. The [Windows package workflow](../../.github/workflows/windows-package.yml)
builds all three shapes, smoke-tests each payload's installation, update and
lifecycle with `smoke.py` (the minimal server package also provisions real
managed STT and both TTS recipes, including repeated Chatterbox setup, through
`smoke.py --speech`, with disposable data and without model weights or
inference), compiles the per-user installer, and runs it through silent
installation and native uninstall in a disposable runner with
`smoke_installer.ps1`, checking the recorded target, startup selection, server
shutdown and preserved user data. It uploads the installer, archive, signature,
runtime inventory and `vbot-release.json` as workflow artifacts and publishes
nothing by itself. Two workflows call it in signed mode:

- [`main-build.yml`](../../.github/workflows/main-build.yml) builds the commit of
  every push to `main` that passed CI with `--channel main` and replaces the assets
  of the rolling `main-build` prerelease, which `install.ps1 -Main` installs and
  main-channel installations update from. It is not a release.
- [`release.yml`](../../.github/workflows/release.yml) builds with `--channel release`
  alongside the Linux packages and the complete CI, checks the complete asset set
  with `scripts/release_assets.py`, and only then creates the tag and the GitHub
  release with every asset attached. The release tag must match the application
  version.

A missing signing key blocks publication. The public PowerShell installer reads
`vbot-release.json` from the release downloads (never the GitHub API), derives
the release tag and the exact installer name from its version, requires the SHA-256 digest it records
and an HTTPS `github.com` download URL, and rejects an invalid Authenticode
signature before executing it.

## Installer and existing installations

The Inno installer is per-user, defaults to `%LOCALAPPDATA%/Programs/vBot`,
registers the command path and normal Windows uninstall entry, and optionally
starts vBot at sign-in. Server data defaults to `~/.vbot`. Silent entrypoints
accept `/DIR`, `/VBOTDATA`, `/VBOTHOST`, `/VBOTPORT`, and `/TASKS=startup` (or an
empty task list). A Desktop Client never controls a default local server.

Prefer the default or another short installation directory. Native bootstrap
runtime paths must fit within 260 characters. Deep dependency paths additionally
require Windows' [long-path policy](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)
when they exceed that limit; the hosts include the corresponding manifest, but
the installer does not change machine-wide policy. The standard payloads fit
below the legacy limit at the default location used in native verification.

The installer requires an empty destination directory. An existing installation
is updated with `vbot update` from its channel; rerunning an installer over it is
refused. `vbot application channel main|release` switches the channel of an
existing installation. Uninstall preserves server data by default.

For Extension dependencies in an installation, see
[the user guide](../../USAGE.md#extension-dependencies-in-a-packaged-installation).
