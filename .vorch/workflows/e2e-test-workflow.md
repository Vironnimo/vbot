# E2E Test Workflow

Run the Playwright E2E suite only when the user explicitly requests E2E test execution. A request to implement, verify, live-test, or run the normal quality gates does not implicitly authorize this suite.

## Scope and Current Baseline

The suite lives in `tests/e2e/` as its own Node package and exercises the built WebUI against a dedicated vBot server and fake Provider. It is intentionally not part of `scripts/quality.py` or `scripts/quality-frontend.py`. `.github/workflows/e2e.yml` installs its clean CI prerequisites and runs the complete Chromium suite on manual dispatch and weekly schedule; this separate observational workflow does not gate pushes or releases while the WebUI is changing, and it uploads failure evidence for seven days.

Some tests may currently fail because the WebUI is still changing. Report those results accurately, but do not fix, skip, delete, or rebaseline failing tests unless the user explicitly asks to stabilize or update the E2E suite. Known E2E failures do not block unrelated work.

## Prerequisites

For a local agent run, start from the repository root. The current Python environment, `webui/node_modules/`, `tests/e2e/node_modules/`, and the Playwright Chromium browser must already be available. Do not install missing dependencies or browsers as part of a local E2E run; report the missing prerequisite as blocked. The GitHub workflow is the clean-run exception: it deliberately installs the locked npm dependencies, Chromium, and required Linux libraries on its disposable runner before executing the suite.

The suite owns ports `8437` for vBot and `8438` for its fake Provider by default. Override them with `VBOT_E2E_PORT` and `VBOT_E2E_PROVIDER_PORT` when either port is unavailable, and use `VBOT_E2E_PYTHON` when the required Python interpreter is not the platform default. Never point the suite at product data or a user-owned server.

## Run the Suite

Run the full suite with the HTML report disabled by its checked-in Playwright configuration:

```bash
npm --prefix tests/e2e test
```

Run a specific spec by passing its path relative to the E2E package:

```bash
npm --prefix tests/e2e test -- tests/primary-navigation.spec.js
```

Global setup recreates only `tests/e2e/.data/` and `tests/e2e/.resources/`, starts the dedicated fake Provider, builds the WebUI, and starts vBot on the dedicated target. Global teardown stops both test-owned processes. The suite uses no real Provider credentials.

## Evidence and Failures

Playwright writes its HTML report, traces, screenshots, videos, and test results under `tests/e2e/artifacts/`; these artifacts are local and git-ignored. Report the command, target ports, passed and failed specs, and the relevant artifact paths. For each failure, distinguish an outdated expectation or locator from an observed product regression when the evidence allows it; otherwise report the ambiguity without guessing.

Do not treat a red suite as permission to change application code or tests. Preserve the artifacts needed to diagnose the run and follow the user's requested scope.

## Cleanup After Interruption

Normal runs clean up through Playwright global teardown. After an interrupted or aborted run, explicitly stop the dedicated vBot server and fake Provider:

```bash
npm --prefix tests/e2e run cleanup
```

Confirm that both dedicated ports are released. Never stop a process on a different target as part of this cleanup.
