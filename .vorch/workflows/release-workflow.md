# Release Workflow

How to cut a tagged GitHub release of vBot. Releases are how end users install: the one-line bootstrap and `vbot update` both consume the release tag **and** its prebuilt WebUI asset. Follow these steps exactly — the notes format and the attached asset are not optional.

## When a release is needed

- **Needed** for any change to **installed code** — anything that ships inside the cloned/installed tree: `core/`, `server/`, `cli/`, `desktop/`, `webui/`, or `scripts/install.{sh,ps1}`.
- **Not needed** for **bootstrap-script-only** changes (`scripts/bootstrap.{sh,ps1}`). The one-liner always fetches `bootstrap.sh` / `bootstrap.ps1` from `main`, so those take effect on push, without a release.

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

### 4. Create the release with auto-generated notes

**Always `--generate-notes`. Never hand-write `--notes`.** The house style is the single auto-generated line GitHub produces:
`**Full Changelog**: https://github.com/Vironnimo/vbot/compare/<prev>...vX.Y.Z` (the previous tag is selected automatically).

```bash
gh release create vX.Y.Z --target main --title "vX.Y.Z" --generate-notes
```

Tag and title are both `vX.Y.Z` (leading `v`); the tag must equal the `pyproject.toml` version.

### 5. Verify the workflow ran and the asset attached

Publishing fires `.github/workflows/release.yml`, which builds the WebUI and attaches `webui-dist.tar.gz`. The bootstrap and `vbot update` fail without it, so confirm it landed (the run takes a few seconds to register after publishing):

```bash
gh run list --workflow=release.yml --limit 3            # find the run for vX.Y.Z
gh run watch <run-id> --exit-status                      # wait until it succeeds
gh release view vX.Y.Z --json tagName,assets --jq '{tag: .tagName, assets: [.assets[].name]}'
```

Expect: the run succeeds, `assets` includes `webui-dist.tar.gz`, and `releases/latest` now resolves to `vX.Y.Z`.

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
- **Publish, not draft**: the workflow triggers on a *published* release. `gh release create` publishes by default — don't pass `--draft`.
- **Bootstrap-only changes need no release** — see "When a release is needed".
