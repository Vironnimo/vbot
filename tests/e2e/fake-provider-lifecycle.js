import {
  closeSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { spawn } from "node:child_process";
import http from "node:http";

import { environment } from "./environment.js";

const SERVICE_NAME = "vbot-e2e-fake-provider";
const START_TIMEOUT_MILLISECONDS = 5_000;
const POLL_INTERVAL_MILLISECONDS = 100;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function fakeProviderIsReady() {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (ready) => {
      if (!settled) {
        settled = true;
        resolve(ready);
      }
    };
    const request = http.get(
      {
        host: environment.fakeProviderHost,
        path: "/health",
        port: environment.providerPort,
        timeout: 500,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            finish(
              response.statusCode === 200 && body?.service === SERVICE_NAME,
            );
          } catch {
            finish(false);
          }
        });
      },
    );
    request.on("error", () => finish(false));
    request.on("timeout", () => {
      request.destroy();
      finish(false);
    });
  });
}

async function waitForReady(expected) {
  const deadline = Date.now() + START_TIMEOUT_MILLISECONDS;
  while (Date.now() < deadline) {
    if ((await fakeProviderIsReady()) === expected) {
      return;
    }
    await delay(POLL_INTERVAL_MILLISECONDS);
  }
  throw new Error(
    `Fake provider did not become ${expected ? "ready" : "stopped"} in time`,
  );
}

function readProviderPid() {
  try {
    const pid = Number.parseInt(
      readFileSync(environment.fakeProviderPidFile, "utf8").trim(),
      10,
    );
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

export async function stopFakeProvider() {
  const pid = readProviderPid();
  const ownsReadyProvider = pid !== null && (await fakeProviderIsReady());
  if (ownsReadyProvider) {
    try {
      process.kill(pid, "SIGTERM");
    } catch (error) {
      if (error?.code !== "ESRCH") {
        throw error;
      }
    }
    await waitForReady(false);
  }
  rmSync(environment.fakeProviderPidFile, { force: true });
}

export async function startFakeProvider() {
  await stopFakeProvider();
  if (await fakeProviderIsReady()) {
    throw new Error(
      `Dedicated fake-provider port ${environment.providerPort} is already in use`,
    );
  }

  const logDescriptor = openSync(environment.fakeProviderLogFile, "w");
  let child;
  try {
    child = spawn(process.execPath, [environment.fakeProviderEntry], {
      detached: true,
      env: {
        ...process.env,
        VBOT_E2E_PROVIDER_HOST: environment.fakeProviderHost,
        VBOT_E2E_PROVIDER_PORT: String(environment.providerPort),
      },
      stdio: ["ignore", logDescriptor, logDescriptor],
      windowsHide: true,
    });
  } finally {
    closeSync(logDescriptor);
  }

  child.unref();
  writeFileSync(environment.fakeProviderPidFile, String(child.pid), "utf8");

  try {
    await waitForReady(true);
  } catch (error) {
    await stopFakeProvider();
    throw error;
  }
}
