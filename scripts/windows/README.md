# Native Windows application packages

`scripts/build_windows.py` builds three x86-64 application shapes: `server`,
`server-desktop`, and `desktop-client`. The application uses readable Python
sources and a private CPython 3.13 runtime. Native hosts load CPython in-process;
the stable root `vBot.exe` starts the tray with no arguments and the CLI with
arguments. Its stable `vBot.GUI.exe` companion uses the Windows GUI subsystem;
the Desktop shortcut invokes it with `desktop` and follows `active-version`.
Install/update integration adds the companion and repairs the owned default
Desktop shortcut while preserving customized targets. Desktop is independently launched and does not own server lifetime.
No Windows service is installed.

## Build inputs and outputs

Use a clean source checkout, a full CPython 3.13 x86-64 runtime with `venv` and
`ensurepip`, LLVM's `clang-cl` and `llvm-rc` with the Windows SDK/linker libraries,
Node.js/npm for server WebUI assets, and Inno Setup 6 for the installer. The
[Windows package workflow](../../.github/workflows/windows-package.yml) records
the CI build sequence for all shapes. Runtime requirements are pinned and hashed
per shape; see [requirements.md](requirements.md) for regeneration. All shapes
use Python 3.13 because the existing
[webrtcvad-wheels audio dependency](https://pypi.org/project/webrtcvad-wheels/2.0.14/#files)
has Windows wheels through Python 3.13. The builder rejects a runtime from another
Python minor version.

For example, from the repository root after building the WebUI:

```powershell
$revision = git rev-parse HEAD
python scripts/build_windows.py --source . --runtime C:\Python313 --output build/windows --shape server --version 0.1.0 --revision $revision --provision-dependencies
```

The runtime argument is an input directory: the builder copies it and provisions
the copied runtime's dependency directory from the selected lock file. It does
not install vBot or its packages into the supplied interpreter. The version
argument must be the version being built, not the example value above.
Use a short build output path for Inno compilation: deeply nested build roots can
exceed the compiler's file path limit even when the native payload itself works.

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
the installer. Keep this private key in release secrets. The installer stores
only the public key; normal official updates fail closed without it. Optional
`--authenticode-command` integrates an externally configured executable-signing
tool. A development build without a release key is usable with an explicitly
selected local `vbot update --package <archive>`.

The package workflow builds, smoke-tests and uploads artifacts; it does not
publish a GitHub release by itself. The Release workflow calls it in signed mode
alongside the repository's existing release checks. Publication waits for both,
then attaches each shape's installer, archive and matching signature together
with the tested WebUI. Before upload, each compiled installer also runs through
silent installation and native uninstall in a disposable CI runner, checking its
recorded target, startup selection, server shutdown and preserved user data.
The minimal server package also provisions real managed STT and both TTS recipes,
including repeated Chatterbox setup, through `smoke.py --speech`. This required
check uses disposable data and verifies imports without model weights or inference.
A missing signing key blocks publication. The release tag
must match the application version. The public PowerShell
installer selects the exact installer name and verifies GitHub's release-asset
SHA256 digest before executing it. Missing binary assets produce an actionable
failure; source installation requires explicit `-SourceCheckout`.

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

The installer requires an empty destination directory. An existing packaged
application is updated with `vbot update`; rerunning an installer over its active
installation is refused. Existing source installations
retain their source updater. Moving one into a packaged installation is explicit:
the payload installer supports `application install ... --from-checkout <path>`,
verifies its install manifest and clean Git state, preserves its exact data/port,
and retires its owned source Autostart only after package registration succeeds.
It preserves the old checkout. Local source edits must first be preserved and
reconciled into the managed development copy; no dirty checkout is discarded.

Windows `-Dev` is also a native installation mode. The public PowerShell installer
first installs the signed base with no startup task, selects main through
`application source main`, waits for the normal durable update, and only then
enables requested Autostart. The main Git checkout lives at `<install>/source`;
`source-update.json` records its branch and remote. Explicit `--from-checkout`
bindings preserve an existing checkout and its track. The running server uses a
complete prepared version, so Git/build failures leave the active version intact.
An initial build failure retains the base installation for an ordinary update retry.
Host input fingerprints in `release.json` permit unchanged native hosts to be reused;
changed native sources require the compiler/SDK inputs described above.

For local features, Extension dependencies and isolated candidate testing, see
[the user guide](../../USAGE.md#local-features-in-a-packaged-windows-application).
Uninstall preserves server data by default and retains development copies so
user-authored features are not deleted as application cache.
