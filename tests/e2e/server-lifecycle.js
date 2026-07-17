import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

import { environment } from "./environment.js";
import {
  startFakeProvider,
  stopFakeProvider,
} from "./fake-provider-lifecycle.js";

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

function prepareTestResources() {
  rmSync(environment.resourcesDir, { force: true, recursive: true });
  cpSync(
    path.join(environment.repoRoot, "resources"),
    environment.resourcesDir,
    {
      recursive: true,
    },
  );

  writeFileSync(
    path.join(environment.resourcesDir, "providers", "fake.json"),
    `${JSON.stringify(
      {
        id: "fake",
        name: "E2E Fake Provider",
        adapter: "openai_compatible",
        base_url: `http://${environment.fakeProviderHost}:${environment.providerPort}/v1`,
        connections: [{ id: "local", type: "none", label: "Local E2E" }],
        defaults: { max_tokens: 512, temperature: 0 },
      },
      null,
      2,
    )}\n`,
    "utf8",
  );

  const model = (name) => ({
    name,
    connections: ["local"],
    context_window: 65_536,
    max_output_tokens: 1_024,
    capabilities: {
      input_modalities: ["text"],
      json_mode: false,
      output_modalities: ["text"],
      reasoning: { supported: false },
      supported_parameters: [],
      task_types: ["chat", "text_output"],
      tools: false,
      vision: false,
    },
  });
  writeFileSync(
    path.join(environment.resourcesDir, "models", "fake.json"),
    `${JSON.stringify(
      {
        fetched_at: "2026-07-17T00:00:00+00:00",
        models: {
          "e2e-primary": model("E2E Primary"),
          "e2e-fallback": model("E2E Fallback"),
        },
        provider_id: "fake",
        source: "e2e",
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

function prepareTestData() {
  rmSync(environment.dataDir, { force: true, recursive: true });
  mkdirSync(environment.dataDir, { recursive: true });
  writeFileSync(
    path.join(environment.dataDir, "settings.json"),
    `${JSON.stringify(
      {
        defaults: {
          agent: {
            fallback_model: "fake/e2e-fallback::local",
            model: "fake/e2e-primary::local",
          },
        },
        providers: { connections: { "fake:local": true } },
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

export default async function startServer() {
  assertDisposableDirectory(environment.dataDir);
  assertDisposableDirectory(environment.resourcesDir);

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

  await stopFakeProvider();
  prepareTestResources();
  prepareTestData();
  await startFakeProvider();

  try {
    execFileSync(environment.python, lifecycleArguments("start"), {
      cwd: environment.repoRoot,
      env: { ...process.env, RESOURCES_PATH: environment.resourcesDir },
      stdio: "inherit",
    });
  } catch (error) {
    await stopFakeProvider();
    throw error;
  }
}
