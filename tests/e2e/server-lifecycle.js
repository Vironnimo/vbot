import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

import { environment } from "./environment.js";

function lifecycleArguments(command) {
  return [
    path.join(environment.repoRoot, "scripts", "test-env.py"),
    command,
    "--host",
    environment.host,
    "--port",
    String(environment.port),
    "--data-dir",
    environment.dataDir,
  ];
}

function assertDisposableDirectory(directory) {
  const relativePath = path.relative(environment.e2eRoot, directory);
  if (
    relativePath === "" ||
    relativePath.startsWith("..") ||
    path.isAbsolute(relativePath)
  ) {
    throw new Error(`Refusing to reset non-E2E directory: ${directory}`);
  }
}

function prepareTestData() {
  rmSync(environment.dataDir, { force: true, recursive: true });
  mkdirSync(environment.dataDir, { recursive: true });
  const settings = JSON.parse(
    readFileSync(
      path.join(environment.e2eRoot, "fake-provider-settings.json"),
      "utf8",
    ),
  );
  settings.providers.custom.fake.base_url =
    `http://${environment.fakeProviderHost}:${environment.providerPort}/v1`;
  writeFileSync(
    path.join(environment.dataDir, "settings.json"),
    `${JSON.stringify(settings, null, 2)}\n`,
    "utf8",
  );
}

export default async function startServer() {
  assertDisposableDirectory(environment.dataDir);

  const stopResult = spawnSync(environment.python, lifecycleArguments("stop"), {
    cwd: environment.repoRoot,
    encoding: "utf8",
  });
  if (stopResult.error) {
    throw stopResult.error;
  }
  if (stopResult.status !== 0) {
    throw new Error(
      `Could not clear the dedicated E2E port before startup:\n${stopResult.stdout}${stopResult.stderr}`,
    );
  }

  prepareTestData();

  execFileSync(environment.python, lifecycleArguments("start"), {
    cwd: environment.repoRoot,
    env: process.env,
    stdio: "inherit",
  });
}
