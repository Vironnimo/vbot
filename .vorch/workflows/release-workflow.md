# Release Workflow

How to cut a tagged GitHub release of vBot. Release-channel installations install and update from the latest release: Windows installations use their shape's installer and signed update archive, Linux installations their platform's signed server package. Follow these steps exactly - the notes format and attached assets are not optional.

The rolling `main-build` prerelease is not a release. `.github/workflows/main-build.yml` runs on every push to `main` that changes more than development documentation (and on manual dispatch from `main`): it calls the complete reusable CI and, side by side, builds the signed Windows packages in all three shapes and the Linux `linux-aarch64` and `linux-x86_64` server packages with channel `main`, without the release-only Windows smokes (managed speech provisioning, compiled installer). Only when CI and every package passed does it check the complete asset set with `scripts/release_assets.py`, which then records the SHA-256 digest of every other asset in `vbot-release.json` (`--record-digests`), moves the `main-build` tag to the built commit, uploads every package before `vbot-release.json`, and deletes assets the build no longer produces. Main-channel installations (`install.ps1 -Main`, `install.sh --main`) install and update from it; `releases/latest` never resolves to it. This workflow never creates, tags or edits it.

## Steps

### 1. Synchronize release state and choose the version

Fetch remote tags before inspecting the latest release. Never infer the next version from an unrefreshed local tag set:

```bash
git fetch --prune --tags origin
git describe --tags --abbrev=0 --match 'v[0-9]*' origin/main
gh release view --json tagName,publishedAt,url
```

The `--match` skips the rolling `main-build` tag. The reachable remote tag and GitHub's latest published release must agree. Resolve any mismatch before continuing. If the user names a version, use that version after confirming it is a new valid SemVer. If the user does not name a version, increment only the patch component of the confirmed latest release by exactly one (`X.Y.Z` → `X.Y.(Z+1)`); do not infer a minor or major bump from the changes. Never reuse an existing tag.

Model DB maintenance is independent of releases. Do not run `scripts/refresh_model_db.py`, modify `resources/models/`, or include incidental Model DB changes while cutting a release.

### 2. Write the changelog

Write one English file at `changelog/X.Y.Z.md` for this stable release. Read every commit since the previous stable tag, including bodies (`git log --format=full v<prev>..HEAD`), and the diff when a subject is unclear. Review every Conventional Commits `!` and every new database migration in core and bundled Extensions, including positional and keyword declarations:

```bash
git diff v<prev>..HEAD -- core resources/extensions | grep -nE '^\+.*(Migration\(|breaks_older)'
```

Start the file with `# vX.Y.Z - YYYY-MM-DD` using the UTC release date. Use these exact level-two headings in this order, omitting empty sections.

1. **Before you update.** Explain who is affected, what changes, and what to do before installing. Include user-facing breaks, changed defaults, renamed or removed settings and commands, manual steps, urgent security fixes and every new `breaks_older=True` migration. Name each affected database: older versions refuse it afterwards, and going back requires restoring its pre-update data snapshot, losing everything written to that database since. Review each marked break for actual user impact. An Extension API break affecting third-party authors goes under Improvements as **Extensions**; omit purely internal breaks.
2. **Highlights.** One to three changes users will notice in daily use. Skip when nothing stands out.
3. **Improvements** and **Fixes.** Group by area, such as Chat, Agents, Desktop or Installation. Use Markdown bullets, one line per user-visible effect. Combine related commits. Describe the problem fixed or the new capability in the user's terms.
4. Omit internal refactors, tests, docs, CI, tooling, merge commits, reverts and changes reverted within this release, Model DB maintenance and implementation details. A five-fix release should fit on one screen.

Run `python scripts/release_changelog.py check X.Y.Z`. The publisher adds the compare link. The rolling main build has no release entry. The published JSON asset contains at most 50 releases and 256 KiB; the GitHub release archive links to the rest.

### 3. Bump the version

The version lives in exactly **one** place: `pyproject.toml` → `version`. Bump it (semver):

```toml
version = "X.Y.Z"
```

### 4. Check the latest CI run on `main`; do not run the complete suite locally

`main-build.yml` runs the complete CI, together with the main packages, on every push to `main`. Check the latest push run before releasing, and wait for it while it is still running (`gh run watch <run-id> --exit-status`):

```bash
gh run list --workflow=main-build.yml --branch=main --event=push --limit 1
```

A run cancelled because a newer push superseded it says nothing; check the newer one. A push that changed only development documentation starts no run, so the latest run may belong to an earlier commit.

If it failed, read the failures (`gh run view <run-id> --log-failed`), fix them on `main`, and push the fix (`.vorch/workflows/push-workflow.md`), whose CI run must pass before you continue; a `main` already known to be red only fails the release gate after its full run time. A first-run backend test failure blocks the release even when its isolated diagnostic retry passes; fix the original failure before dispatching again.

The push in step 5 runs the complete local checks and starts the `main-build` run of the release commit. The dispatched Release workflow does not run the CI a second time: its `main-build CI` job waits for that run and blocks tag and Release creation until its CI passed, while the release packages build. If that CI fails, fix the reported problem on `main`, push it, and dispatch the Release workflow again.

### 5. Commit and push

```bash
git add pyproject.toml changelog/X.Y.Z.md
git commit -m "chore(release): bump version to X.Y.Z"
```

Then push `main` through `.vorch/workflows/push-workflow.md`. Dispatch the release (step 6) as soon as `python scripts/push.py` has pushed, without waiting for the `main-build` run first: the Release workflow waits for it itself. Still follow that run to its end as the push workflow's step 4 requires, alongside the release run in step 7.

### 6. Dispatch the gated release workflow

Dispatch `.github/workflows/release.yml` from `main`. Pass the version without the leading `v`:

```bash
gh workflow run release.yml --ref main -f version=X.Y.Z
```

After the changelog check passes, the workflow runs three gates in parallel. `ci` (`main-build CI`) runs no tests itself: `scripts/release_gate.py` waits for the `main-build` run of the dispatched commit and passes once one of its runs completed with every job of the reusable `.github/workflows/ci.yml` successful. It fails as soon as that CI failed or was cancelled, and when no run starts within ten minutes; a commit whose push changed only development documentation starts none, so start one with `gh workflow run main-build.yml --ref main`, then dispatch the release again. That CI covers the static checks (Ruff format and lint, mypy) on Linux x64 and Windows; the complete backend test suite on Linux x64, Linux ARM64 and Windows (three shards), all with the one supported Python version (`PROJECT.md` -> Development -> Python version); and the WebUI format, lint, test and build steps. The E2E suite is not part of it. It is also manually dispatchable. All backend test jobs, including the Windows shards, inherit the shared `-n auto --dist=worksteal` configuration and use the runner-selected worker count without a platform-specific cap; failed backend tests run once more alone for diagnosis, but the first failure still fails the job even when the retry passes. Keep this policy for subsequent CI and release runs; resolve test isolation or readiness failures in their owning fixtures instead of reducing Windows concurrency. Large Session/participant fixtures have explicit 120-second test budgets.

`windows` and `linux` call `windows-package.yml` and `linux-package.yml` in signed release mode with channel `release`. The Windows workflow builds all three shapes (`server`, `server-desktop`, `desktop-client`) with locked CPython dependencies, exercises disposable payload installation, update and lifecycle (the minimal server package additionally provisions managed STT and both TTS recipes, including repeated Chatterbox setup, without model weights or inference), compiles the per-user Inno installers, and runs each compiled installer and uninstaller through `scripts/windows/smoke_installer.ps1` in a disposable CI runner, checking its target, startup selection, server shutdown and preserved data. The Linux workflow builds the `linux-aarch64` and `linux-x86_64` server packages, each on a runner of its own architecture, and exercises installation, update, lifecycle and uninstall with `scripts/linux/smoke.py`. Configure `VBOT_RELEASE_SIGNING_KEY` as a repository secret containing a base64 raw Ed25519 private key; the builds sign every update archive with it and embed only its public key. A missing key or package failure blocks publication. See `scripts/windows/README.md` and `scripts/linux/README.md` for the build contracts.

The private release key is absent from build runners, dependency installation, native compilation and all smokes. After those pass, each package workflow downloads the unchanged tested artifacts into a fresh signing job. Only `python -I scripts/sign_packages.py` receives `VBOT_RELEASE_SIGNING_KEY`; it signs opaque archives without importing or running the payload and refuses a private key that differs from the public identity in `scripts/install.sh`. The Windows installers embed that same public key during their keyless construction. Publishers download only final `vbot-*` artifacts, never intermediate `unsigned-*` artifacts.

Only after all three gates pass does the `publish` job validate the release metadata, download every package, and check the complete asset set with `scripts/release_assets.py --channel release`: every expected asset must be present, and `vbot-release.json` must name `X.Y.Z`, the dispatched commit and channel `release`. With `--record-digests` it then adds the SHA-256 digest of every other asset to `vbot-release.json`, which the Installers verify their downloads against; it fails instead when the identity would exceed the 4096 bytes installed updaters accept. It then creates `vX.Y.Z` at the exact dispatched commit with all assets attached; it never rebuilds or repackages a package.

After publication, the workflow calls `.github/workflows/release-smoke.yml` as a thin Public Distribution canary. It checks the public tag format and the published asset names with `scripts/release_assets.py --names`, then exercises both public Installers against the exact new tag. On Linux ARM64 and Linux x64, `install.sh --version vX.Y.Z --no-autostart` installs the package; the canary checks the installed version and the `~/.local/bin/vbot` link, starts the server, probes `/health` and the WebUI, stops it, runs `vbot uninstall --app-only --yes`, and confirms that the installation and link are gone and the data directory survived. On Windows, `install.ps1 -Version vX.Y.Z -NoAutostart` installs the server shape; the canary checks the installed version, starts the server, probes `/health` and the WebUI, runs the native uninstaller, and confirms that product data survived. Publication must happen first because the Installers consume the real GitHub Release and its assets; therefore a canary failure still marks the Release workflow red but cannot unpublish the already-created release. Package behavior belongs in the pre-publish package smokes; the canary should expose only public acquisition discrepancies. The smoke workflow remains manually dispatchable for any existing release tag.

To re-run only the public-distribution smoke test without creating or changing a release:

```bash
gh workflow run release-smoke.yml --ref main -f tag=vX.Y.Z
```

The workflow validates that `X.Y.Z` is SemVer, equals `pyproject.toml` → `version`, and does not already exist as a tag. It publishes the Markdown file followed by a blank line and `**Full Changelog**: https://github.com/Vironnimo/vbot/compare/v<prev>...vX.Y.Z`. The previous stable tag is the nearest reachable from the release commit's parent. The publish job builds `vbot-changelog.json` before recording the asset digests. All migration notices ship with the release.

### 7. Verify the workflow ran and the assets attached

Wait for the dispatched workflow and confirm the release and its assets landed. The Installers and `vbot update` fail without them:

```bash
gh run list --workflow=release.yml --limit 3            # find the run for vX.Y.Z
gh run watch <run-id> --exit-status                      # wait until it succeeds
gh release view vX.Y.Z --json assets --jq '.assets[].name' > assets.txt
python scripts/release_assets.py --names assets.txt --version X.Y.Z --channel release
gh release view --json tagName --jq .tagName             # the latest release
```

Expect: the `main-build CI` gate and all Windows package, Linux package, publish and Public Distribution canary jobs succeed; `release_assets.py` prints nothing and exits 0, so the release carries, for each `server`, `server-desktop` and `desktop-client` shape, `vBot-X.Y.Z-windows-x86_64-<shape>.exe`, `vbot-windows-x86_64-<shape>.zip`, its `.zip.sig` and `vbot-windows-x86_64-<shape>-runtime-inventory.json`; for `linux-aarch64` and `linux-x86_64`, `vbot-<platform>-server.zip`, its `.zip.sig` and `vbot-<platform>-server-runtime-inventory.json`; the release-only `vbot-changelog.json`; and `vbot-release.json`, which records the SHA-256 digest of each of the others. The latest release resolves to `vX.Y.Z`.

## Correcting published notes

Correct the version's Markdown file on `main` and commit the correction. Regenerate the body using the previous stable tag and update the GitHub release:

```bash
python scripts/release_changelog.py notes X.Y.Z --previous vA.B.C > release-notes.md
gh release edit vX.Y.Z --notes-file release-notes.md
```

The published asset and packages retain their original bytes and digests. The next stable release includes the correction in its asset and bundled history.

## Gotchas

- **Default version bump**: when the user does not name a version, bump only the patch component of the synchronized latest release by one. Change scope does not override this default.
- **Assets are mandatory**: Windows requires each shape's installer and signed update archive, Linux each platform's signed server package, and the Installers and updaters read `vbot-release.json` (version and asset digests) from the release downloads. `scripts/release_assets.py` defines the complete set. Never skip step 7.
- **Package identity**: the package workflows build and smoke-test each package once; publish downloads and attaches exactly those files without rebuilding or repackaging.
- **Post-publish scope**: the Public Distribution canary exists because the real public GitHub tag and assets cannot be acquired before publication. Package behavior belongs in the pre-publish gates; the canary only proves the final public acquisition path.
- **`main-build` is not a release**: never cut a release from it or tag it `vX.Y.Z`; `main-build.yml` owns that prerelease and republishes it after every push to `main` whose CI and packages passed.
- **Tag = version**: `vX.Y.Z` must equal the `pyproject.toml` version, with a leading `v`.
- **Remote state first**: fetch tags and confirm GitHub's latest release before choosing the version; a stale local tag set is not release evidence.
- **Model DB is separate**: never refresh or stage `resources/models/` as part of a release.
- **Publication is last**: never create the tag or GitHub Release manually. The workflow publishes both only after the full CI gate and the signed Windows and Linux packages succeed and the asset set is complete.
