# Release Workflow

How to cut a tagged GitHub release of vBot. Releases are how end users install: the public `install.sh`/`install.ps1` entrypoints and `vbot update` consume the release tag **and** its prebuilt WebUI asset. Follow these steps exactly — the notes format and the attached asset are not optional.

## Steps

### 1. Synchronize release state and choose the version

Fetch remote tags before inspecting the latest release. Never infer the next version from an unrefreshed local tag set:

```bash
git fetch --prune --tags origin
git describe --tags --abbrev=0 origin/main
gh release view --json tagName,publishedAt,url
```

The reachable remote tag and GitHub's latest published release must agree. Resolve any mismatch before continuing. Choose the next SemVer from that confirmed remote state and the changes since the release; do not reuse an existing tag.

Model DB maintenance is independent of releases. Do not run `scripts/refresh_model_db.py`, modify `resources/models/`, or include incidental Model DB changes while cutting a release.

### 2. Bump the version

The version lives in exactly **one** place: `pyproject.toml` → `version`. Bump it (semver):

```toml
version = "X.Y.Z"
```

### 3. Do not run local quality gates

Release tasks are explicitly exempt from the repository's normal local quality-gate passes. Do not run `scripts/quality.py` or `scripts/quality-frontend.py` before a release: the dispatched GitHub Release workflow calls the complete reusable CI workflow against the pushed `main` commit and blocks tag and Release creation until every required Backend, Frontend, and Installer job passes. If that CI fails, fix the reported problem on `main`, push it, and dispatch the Release workflow again.

### 4. Commit and push

```bash
git add pyproject.toml
git commit -m "chore(release): bump version to X.Y.Z"
git push origin main
```

### 5. Dispatch the gated release workflow

Dispatch `.github/workflows/release.yml` from `main`. Pass the version without the leading `v`:

```bash
gh workflow run release.yml --ref main -f version=X.Y.Z
```

The workflow calls the complete reusable CI workflow first. In parallel where dependencies allow, CI runs the full Backend gate on Linux x64, Linux ARM64, and Windows; the Frontend gate; the complete Chromium E2E suite; and one WebUI release-candidate build. The exact resulting `webui-dist.tar.gz` then feeds pre-publish Candidate Smokes on Linux x64, Linux ARM64, and Windows. Those smokes install the checked-out candidate with the real packaged WebUI, start the server, probe `/health` and the WebUI, uninstall through the recorded interpreter, and verify that product data survived. `.github/workflows/ci.yml` remains callable-only, so this release matrix runs when the Release workflow calls it. Only after every gate passes does the workflow create `vX.Y.Z` at the exact dispatched commit and attach the already-tested `webui-dist.tar.gz`; the publish job never rebuilds or repackages the candidate.

After publication, the workflow calls `.github/workflows/release-smoke.yml` as a thin Public Distribution canary. It validates the public tag and mandatory asset, then exercises the two public Installer implementations on Linux x64 and Windows against the exact new tag: install from GitHub, validate the checked-out tag and project version, start the installed server, probe `/health` and the WebUI, uninstall, and confirm that product data survived. Linux ARM64 behavior is already covered by the pre-publish Candidate Smoke, while the Unix public acquisition path is shared with Linux x64. Publication must happen first because the Installer consumes the real GitHub Release and asset; therefore a canary failure still marks the Release workflow red but cannot unpublish the already-created release. The canary should now expose only public GitHub acquisition discrepancies rather than first discovering candidate runtime or platform-install failures. The smoke workflow remains manually dispatchable for any existing release tag.

To re-run only the public-distribution smoke test without creating or changing a release:

```bash
gh workflow run release-smoke.yml --ref main -f tag=vX.Y.Z
```

The workflow validates that `X.Y.Z` is SemVer, equals `pyproject.toml` → `version`, and does not already exist as a tag. It creates auto-generated notes; never replace them with hand-written notes. The house style is the single auto-generated line GitHub produces:
`**Full Changelog**: https://github.com/Vironnimo/vbot/compare/<prev>...vX.Y.Z` (the previous tag is selected automatically).

### 6. Verify the workflow ran and the asset attached

Wait for the dispatched workflow and confirm the release and asset landed. The Installer and `vbot update` fail without the asset:

```bash
gh run list --workflow=release.yml --limit 3            # find the run for vX.Y.Z
gh run watch <run-id> --exit-status                      # wait until it succeeds
gh release view vX.Y.Z --json tagName,assets --jq '{tag: .tagName, assets: [.assets[].name]}'
```

Expect: all Backend, Frontend, E2E, Candidate Build, Candidate Smoke, publish, and Public Distribution canary jobs succeed; `assets` includes the gated `webui-dist.tar.gz`; and `releases/latest` resolves to `vX.Y.Z`.

### 7. ONE TIME FOR THE NEXT RELEASE: convert the current installation's data layout and attachment blobs

This step is mandatory for the next release only. The current Windows installation at `C:\Users\Viro\vbot` still runs both the old root-level data-directory layout and the suffixless attachment-blob layout against `C:\Users\Viro\.vbot`; this implementation deliberately converts only the development directory `C:\Users\Viro\.vbot-dev`, because converting `.vbot` before the new release is installed would make its data unreadable to the old server. After the new GitHub release has passed step 6, stop that installation, make a complete backup, update it without restarting, run the shipped structural converter in dry-run/apply/dry-run order, run the Attachment converter twice, validate configuration, and only then start the new server:

```powershell
vbot server stop
$backup = "C:\Users\Viro\.vbot-before-data-layout-vX.Y.Z"
if (Test-Path -LiteralPath $backup) { throw "Backup target already exists: $backup" }
Copy-Item -LiteralPath "C:\Users\Viro\.vbot" -Destination $backup -Recurse
vbot update --no-restart
& "C:\Users\Viro\vbot\.venv\Scripts\python.exe" "C:\Users\Viro\vbot\scripts\converters\data_dir_artifacts_layout.py" "C:\Users\Viro\.vbot"
& "C:\Users\Viro\vbot\.venv\Scripts\python.exe" "C:\Users\Viro\vbot\scripts\converters\data_dir_artifacts_layout.py" "C:\Users\Viro\.vbot" --apply
& "C:\Users\Viro\vbot\.venv\Scripts\python.exe" "C:\Users\Viro\vbot\scripts\converters\data_dir_artifacts_layout.py" "C:\Users\Viro\.vbot"
& "C:\Users\Viro\vbot\.venv\Scripts\python.exe" "C:\Users\Viro\vbot\scripts\converters\attachment_blob_extensions.py" "C:\Users\Viro\.vbot"
& "C:\Users\Viro\vbot\.venv\Scripts\python.exe" "C:\Users\Viro\vbot\scripts\converters\attachment_blob_extensions.py" "C:\Users\Viro\.vbot"
vbot doctor config --data-dir "C:\Users\Viro\.vbot"
vbot server start
vbot server status
```

Replace `vX.Y.Z` in the backup path with the released version. The first structural run must report the complete move plan without changing disk, apply must succeed without a collision, and the second structural run must report no planned moves. The first Attachment-converter run must succeed; the second must report `converted=0` and `already_converted` equal to the number of Attachment sidecars in `C:\Users\Viro\.vbot\artifacts\attachments`. Configuration validation must pass and `server status` must report the new installation as running. Keep the backup until the converted installation has been verified. Once those checks pass, delete this entire step 7 from this workflow and commit and push that deletion on `main`. The one-time cutover and the next-release task are not complete until this instruction has removed itself.

## Fixing notes after the fact

`gh release edit` has no `--generate-notes`. If a release ends up with the wrong body (e.g. hand-written notes), regenerate the house-style notes via the API and overwrite:

```bash
gh api repos/Vironnimo/vbot/releases/generate-notes \
  -f tag_name=vX.Y.Z -f previous_tag_name=v<prev> --jq .body \
  | gh release edit vX.Y.Z --notes-file -
```

## Gotchas

- **Notes**: only the auto-generated Full Changelog line — no custom prose. A custom `--notes` replaces it and breaks the convention every prior release follows.
- **Asset is mandatory**: a release without `webui-dist.tar.gz` cannot be installed by the public Installer or reached by `vbot update`. Never skip step 6.
- **Candidate identity**: CI builds `webui-dist.tar.gz` once; Candidate Smokes test that exact artifact, and publish downloads and attaches it without rebuilding or repackaging.
- **Post-publish scope**: the Public Distribution canary exists because the real public GitHub tag and asset cannot be acquired before publication. Candidate behavior belongs in the pre-publish gates; the canary only proves the final public acquisition path.
- **Tag = version**: `vX.Y.Z` must equal the `pyproject.toml` version, with a leading `v`.
- **Remote state first**: fetch tags and confirm GitHub's latest release before choosing the version; a stale local tag set is not release evidence.
- **Model DB is separate**: never refresh or stage `resources/models/` as part of a release.
- **Publication is last**: never create the tag or GitHub Release manually. The workflow publishes both only after the full CI gate and WebUI packaging succeed.
