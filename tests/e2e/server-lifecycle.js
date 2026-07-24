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
  rmSync(
    path.join(environment.resourcesDir, "models", "openai.overrides.json"),
    { force: true },
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

  const model = (name, capabilityOverrides = {}) => ({
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
      tools: true,
      vision: false,
      ...capabilityOverrides,
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

  writeFileSync(
    path.join(environment.resourcesDir, "providers", "openai.json"),
    `${JSON.stringify(
      {
        id: "openai",
        name: "E2E Fake OpenAI Task Provider",
        adapter: "openai",
        base_url: `http://${environment.fakeProviderHost}:${environment.providerPort}/v1`,
        connections: [
          {
            id: "local",
            type: "api_key",
            label: "Local E2E",
            auth: {
              header: "x-vbot-e2e-task-key",
              prefix: "",
              credential_key: "VBOT_E2E_FAKE_TASK_KEY",
            },
          },
        ],
        defaults: { max_tokens: 512, temperature: 0 },
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
  writeFileSync(
    path.join(environment.resourcesDir, "models", "openai.json"),
    `${JSON.stringify(
      {
        fetched_at: "2026-07-17T00:00:00+00:00",
        models: {
          "e2e-image": model("E2E Image", {
            output_modalities: ["image"],
            supported_parameters: ["response_format", "output_format"],
            task_types: ["image_generation"],
            tools: false,
          }),
          "e2e-tts": model("E2E Text to Speech", {
            output_modalities: ["speech"],
            supported_parameters: ["voice", "response_format"],
            supported_voices: ["alloy"],
            task_types: ["text_to_speech"],
            tools: false,
          }),
        },
        provider_id: "openai",
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
        model_tasks: {
          image_generation: {
            target: "openai/e2e-image::local",
            options: { output_format: "png", response_format: "b64_json" },
          },
          text_to_speech: {
            target: "openai/e2e-tts::local",
            options: { response_format: "wav", voice: "alloy" },
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
      env: {
        ...process.env,
        RESOURCES_PATH: environment.resourcesDir,
        VBOT_E2E_FAKE_TASK_KEY: "local-e2e-only",
      },
      stdio: "inherit",
    });
  } catch (error) {
    await stopFakeProvider();
    throw error;
  }
}
