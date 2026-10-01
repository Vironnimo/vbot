# Release Workflow

How to cut a tagged GitHub release of vBot. Release-channel installations install and update from the latest release: Windows installations use their shape's installer and signed update archive, Linux installations their platform's signed server package. Follow these steps exactly - the notes format and attached assets are not optional.

The rolling `main-build` prerelease is not a release. `.github/workflows/main-build.yml` republishes it automatically after every green CI run on a push to `main` (or on manual dispatch from `main`): it builds the signed Windows packages in all three shapes and the Linux `linux-aarch64` and `linux-x86_64` server packages with channel `main`, checks the complete asset set with `scripts/release_assets.py`, moves the `main-build` tag to the built commit, uploads every package before `vbot-release.json`, and deletes assets the build no longer produces. Main-channel installations (`install.ps1 -Main`, `install.sh --main`) install and update from it; `releases/latest` never resolves to it. This workflow never creates, tags or edits it.

## Steps

### 1. Synchronize release state and choose the version

Fetch remote tags before inspecting the latest release. Never infer the next version from an unrefreshed local tag set:

```bash
git fetch --prune --tags origin
git describe --tags --abbrev=0 origin/main
gh release view --json tagName,publishedAt,url
```

The reachable remote tag and GitHub's latest published release must agree. Resolve any mismatch before continuing. If the user names a version, use that version after confirming it is a new valid SemVer. If the user does not name a version, increment only the patch component of the confirmed latest release by exactly one (`X.Y.Z` → `X.Y.(Z+1)`); do not infer a minor or major bump from the changes. Never reuse an existing tag.

Model DB maintenance is independent of releases. Do not run `scripts/refresh_model_db.py`, modify `resources/models/`, or include incidental Model DB changes while cutting a release.

### 2. Bump the version

The version lives in exactly **one** place: `pyproject.toml` → `version`. Bump it (semver):

```toml
version = "X.Y.Z"
```

### 3. Check the latest CI run on `main`; do not run the complete suite locally

`ci.yml` runs the complete CI on every push to `main`. Check the latest push run before releasing, and wait for it while it is still running (`gh run watch <run-id> --exit-status`):

```bash
gh run list --workflow=ci.yml --branch=main --event=push --limit 1
```

If it failed, read the failures (`gh run view <run-id> --log-failed`), fix them on `main`, and push the fix (`.vorch/workflows/push-workflow.md`), whose CI run must pass before you continue; a `main` already known to be red only fails the release gate after its full run time. `Flaky tests` warnings in a green run name tests that failed under the parallel load and passed when run again alone; they do not block the release.

The push in step 4 runs the complete local checks; the dispatched GitHub Release workflow then calls the complete reusable CI workflow against the pushed `main` commit and blocks tag and Release creation until every required static, Backend, Frontend, E2E and package job passes. If that CI fails, fix the reported problem on `main`, push it, and dispatch the Release workflow again.

### 4. Commit and push

```bash
git add pyproject.toml
git commit -m "chore(release): bump version to X.Y.Z"
```

Then push `main` through `.vorch/workflows/push-workflow.md`.

### 5. Dispatch the gated release workflow

Dispatch `.github/workflows/release.yml` from `main`. Pass the version without the leading `v`:

```bash
gh workflow run release.yml --ref main -f version=X.Y.Z
```

The workflow runs three gates in parallel. `quality` calls the reusable `.github/workflows/ci.yml`: the static checks (Ruff format and lint, mypy) on Linux x64 and Windows; the complete backend test suite on Linux x64 (Python 3.11, 3.13 and 3.14), Linux ARM64 (Python 3.11) and Windows (Python 3.11 and 3.14, three shards each); the WebUI format, lint, test and build steps; and the complete Chromium E2E suite. `ci.yml` also runs on every push to `main` and is manually dispatchable. All backend test jobs, including the Windows shards, inherit the shared `-n auto --dist=worksteal` configuration and use the runner-selected worker count without a platform-specific cap; failed backend tests run once more alone, and tests that then pass become a warning instead of failing the job. Keep this policy for subsequent CI and release runs; resolve test isolation or readiness failures in their owning fixtures instead of reducing Windows concurrency. Large Session/participant fixtures have explicit 120-second test budgets.

`windows` and `linux` call `windows-package.yml` and `linux-package.yml` in signed release mode with channel `release`. The Windows workflow builds all three shapes (`server`, `server-desktop`, `desktop-client`) with locked CPython dependencies, exercises disposable payload installation, update and lifecycle (the minimal server package additionally provisions managed STT and both TTS recipes, including repeated Chatterbox setup, without model weights or inference), compiles the per-user Inno installers, and runs each compiled installer and uninstaller through `scripts/windows/smoke_installer.ps1` in a disposable CI runner, checking its target, startup selection, server shutdown and preserved data. The Linux workflow builds the `linux-aarch64` and `linux-x86_64` server packages, each on a runner of its own architecture, and exercises installation, update, lifecycle and uninstall with `scripts/linux/smoke.py`. Configure `VBOT_RELEASE_SIGNING_KEY` as a repository secret containing a base64 raw Ed25519 private key; the builds sign every update archive with it and embed only its public key. A missing key or package failure blocks publication. See `scripts/windows/README.md` and `scripts/linux/README.md` for the build contracts.

Only after all three gates pass does the `publish` job validate the release metadata, download every package, and check the complete asset set with `scripts/release_assets.py --channel release`: every expected asset must be present, and `vbot-release.json` must name `X.Y.Z`, the dispatched commit and channel `release`. It then creates `vX.Y.Z` at the exact dispatched commit with all assets attached; it never rebuilds or repackages a package.

After publication, the workflow calls `.github/workflows/release-smoke.yml` as a thin Public Distribution canary. It checks the public tag format and the published asset names with `scripts/release_assets.py --names`, then exercises both public Installers against the exact new tag. On Linux ARM64 and Linux x64, `install.sh --version vX.Y.Z --no-autostart` installs the package; the canary checks the installed version and the `~/.local/bin/vbot` link, starts the server, probes `/health` and the WebUI, stops it, runs `vbot uninstall --app-only --yes`, and confirms that the installation and link are gone and the data directory survived. On Windows, `install.ps1 -Version vX.Y.Z -NoAutostart` installs the server shape; the canary checks the installed version, starts the server, probes `/health` and the WebUI, runs the native uninstaller, and confirms that product data survived. Publication must happen first because the Installers consume the real GitHub Release and its assets; therefore a canary failure still marks the Release workflow red but cannot unpublish the already-created release. Package behavior belongs in the pre-publish package smokes; the canary should expose only public acquisition discrepancies. The smoke workflow remains manually dispatchable for any existing release tag.

To re-run only the public-distribution smoke test without creating or changing a release:

```bash
gh workflow run release-smoke.yml --ref main -f tag=vX.Y.Z
```

The workflow validates that `X.Y.Z` is SemVer, equals `pyproject.toml` → `version`, and does not already exist as a tag. It creates auto-generated notes; never replace them with hand-written notes. The one addition is the data-compatibility notice of step 7, only when that step requires it. The house style is the single auto-generated line GitHub produces:
`**Full Changelog**: https://github.com/Vironnimo/vbot/compare/<prev>...vX.Y.Z` (the previous tag is selected automatically).

### 6. Verify the workflow ran and the assets attached

Wait for the dispatched workflow and confirm the release and its assets landed. The Installers and `vbot update` fail without them:

```bash
gh run list --workflow=release.yml --limit 3            # find the run for vX.Y.Z
gh run watch <run-id> --exit-status                      # wait until it succeeds
gh release view vX.Y.Z --json assets --jq '.assets[].name' > assets.txt
python scripts/release_assets.py --names assets.txt --version X.Y.Z
gh release view --json tagName --jq .tagName             # the latest release
```

Expect: all quality (static, Backend, Frontend, E2E), Windows package, Linux package, publish, and Public Distribution canary jobs succeed; `release_assets.py` prints nothing and exits 0, so the release carries, for each `server`, `server-desktop` and `desktop-client` shape, `vBot-X.Y.Z-windows-x86_64-<shape>.exe`, `vbot-windows-x86_64-<shape>.zip`, its `.zip.sig` and `vbot-windows-x86_64-<shape>-runtime-inventory.json`; for `linux-aarch64` and `linux-x86_64`, `vbot-<platform>-server.zip`, its `.zip.sig` and `vbot-<platform>-server-runtime-inventory.json`; and `vbot-release.json`. The latest release resolves to `vX.Y.Z`.

### 7. Add the data-compatibility notice when a migration breaks older versions

A data migration declared with `breaks_older=True` makes every older vBot refuse the database it ran on (`.vorch/domain-maps/database.md` -> Evolution contract, item 4). Users must be able to read this in the release notes before they update, because going back afterwards means restoring a data snapshot from before the update. Check whether the release adds such a migration to a core owner or a bundled Extension (declarations are `Migration(name, breaks_older, apply)`, keyword or positional), and review every match:

```bash
git diff v<prev>..vX.Y.Z -- core resources/extensions | grep -nE '^\+.*(Migration\(|breaks_older)'
```

When none is added, stop: the notes stay exactly as generated. When one is added, prepend one notice per affected database above the generated line, keeping that line unchanged:

```bash
notice='**Data compatibility**: this release migrates the `<database>` database in a way older vBot versions cannot read. After updating, an older version refuses to open it; going back requires restoring a data snapshot taken before the update, which loses everything written to that database since.'
{ printf '%s\n\n' "$notice"; gh release view vX.Y.Z --json body --jq .body; } \
  | gh release edit vX.Y.Z --notes-file -
```

`<database>` is the kernel database name (`sessions`, `ext.<extension>.<name>`, ...). This notice is the only prose a release body may carry.

## Fixing notes after the fact

`gh release edit` has no `--generate-notes`. If a release ends up with the wrong body (e.g. hand-written notes), regenerate the house-style notes via the API and overwrite, then add the step 7 notice again if the release requires one:

```bash
gh api repos/Vironnimo/vbot/releases/generate-notes \
  -f tag_name=vX.Y.Z -f previous_tag_name=v<prev> --jq .body \
  | gh release edit vX.Y.Z --notes-file -
```

## Gotchas

- **Default version bump**: when the user does not name a version, bump only the patch component of the synchronized latest release by one. Change scope does not override this default.
- **Notes**: only the auto-generated Full Changelog line — no custom prose. A custom `--notes` replaces it and breaks the convention every prior release follows. The single exception is the step 7 data-compatibility notice above that line, required when the release adds a `breaks_older=True` migration and forbidden otherwise.
- **Assets are mandatory**: Windows requires each shape's installer and signed update archive, Linux each platform's signed server package, and updaters read `vbot-release.json`. `scripts/release_assets.py` defines the complete set. Never skip step 6.
- **Package identity**: the package workflows build and smoke-test each package once; publish downloads and attaches exactly those files without rebuilding or repackaging.
- **Post-publish scope**: the Public Distribution canary exists because the real public GitHub tag and assets cannot be acquired before publication. Package behavior belongs in the pre-publish gates; the canary only proves the final public acquisition path.
- **`main-build` is not a release**: never cut a release from it or tag it `vX.Y.Z`; `main-build.yml` owns that prerelease and republishes it after every green push to `main`.
- **Tag = version**: `vX.Y.Z` must equal the `pyproject.toml` version, with a leading `v`.
- **Remote state first**: fetch tags and confirm GitHub's latest release before choosing the version; a stale local tag set is not release evidence.
- **Model DB is separate**: never refresh or stage `resources/models/` as part of a release.
- **Publication is last**: never create the tag or GitHub Release manually. The workflow publishes both only after the full CI gate and the signed Windows and Linux packages succeed and the asset set is complete.
