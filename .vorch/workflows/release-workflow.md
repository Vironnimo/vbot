# Release Workflow

How to cut a tagged GitHub release of vBot. Releases are how end users install: the one-line bootstrap and `vbot update` both consume the release tag **and** its prebuilt WebUI asset. Follow these steps exactly — the notes format and the attached asset are not optional.

## Steps

### 1. Bump the version

The version lives in exactly **one** place: `pyproject.toml` → `version`. Bump it (semver):

```toml
version = "X.Y.Z"
```

### 2. Green gates on `main`

Run both full quality gates (no args) and make them green before tagging — see `.vorch/PROJECT.md` → Quality gates:

```bash
python scripts/quality.py
python scripts/quality-frontend.py
```

### 3. Commit and push

```bash
git add pyproject.toml
git commit -m "chore(release): bump version to X.Y.Z"
git push origin main
```

### 4. Dispatch the gated release workflow

Dispatch `.github/workflows/release.yml` from `main`. Pass the version without the leading `v`:

```bash
gh workflow run release.yml --ref main -f version=X.Y.Z
```

The workflow calls the complete reusable CI workflow first, covering the full Backend gate on Linux x64, Linux ARM64, and Windows; the Frontend gate; Linux x64 and ARM64 install/uninstall; and Windows install/uninstall. Only after all seven job instances pass does it build the WebUI and create `vX.Y.Z` at the exact commit that was dispatched. The release is created with the mandatory `webui-dist.tar.gz` already attached, so GitHub never exposes an incomplete installable release.

After publication, the workflow calls `.github/workflows/release-smoke.yml`. That reusable workflow downloads the public bootstrap from the dispatched commit, installs the exact new tag from GitHub on Linux x64, Linux ARM64, and Windows, validates the checked-out tag and project version, starts the installed server, probes `/health` and the WebUI, then runs the bootstrap uninstaller and verifies that product data survived. Publication must happen first because the bootstrap consumes the real GitHub Release and asset; therefore a smoke failure marks the Release workflow red but cannot unpublish the already-created release. The smoke workflow is also manually dispatchable for any existing release tag.

To re-run only the public-distribution smoke test without creating or changing a release:

```bash
gh workflow run release-smoke.yml --ref main -f tag=vX.Y.Z
```

The workflow validates that `X.Y.Z` is SemVer, equals `pyproject.toml` → `version`, and does not already exist as a tag. It creates auto-generated notes; never replace them with hand-written notes. The house style is the single auto-generated line GitHub produces:
`**Full Changelog**: https://github.com/Vironnimo/vbot/compare/<prev>...vX.Y.Z` (the previous tag is selected automatically).

### 5. Verify the workflow ran and the asset attached

Wait for the dispatched workflow and confirm the release and asset landed. The bootstrap and `vbot update` fail without the asset:

```bash
gh run list --workflow=release.yml --limit 3            # find the run for vX.Y.Z
gh run watch <run-id> --exit-status                      # wait until it succeeds
gh release view vX.Y.Z --json tagName,assets --jq '{tag: .tagName, assets: [.assets[].name]}'
```

Expect: all CI, publish, and release-smoke jobs succeed; `assets` includes `webui-dist.tar.gz`; and `releases/latest` resolves to `vX.Y.Z`.

## Fixing notes after the fact

`gh release edit` has no `--generate-notes`. If a release ends up with the wrong body (e.g. hand-written notes), regenerate the house-style notes via the API and overwrite:

```bash
gh api repos/Vironnimo/vbot/releases/generate-notes \
  -f tag_name=vX.Y.Z -f previous_tag_name=v<prev> --jq .body \
  | gh release edit vX.Y.Z --notes-file -
```

## Gotchas

- **Notes**: only the auto-generated Full Changelog line — no custom prose. A custom `--notes` replaces it and breaks the convention every prior release follows.
- **Asset is mandatory**: a release without `webui-dist.tar.gz` cannot be installed by the bootstrap or reached by `vbot update`. Never skip step 5.
- **Tag = version**: `vX.Y.Z` must equal the `pyproject.toml` version, with a leading `v`.
- **Publication is last**: never create the tag or GitHub Release manually. The workflow publishes both only after the full CI gate and WebUI packaging succeed.
