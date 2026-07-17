import path from "node:path";
import { fileURLToPath } from "node:url";

const e2eRoot = path.dirname(fileURLToPath(import.meta.url));

function readPort(environmentName, fallback) {
  const port = Number.parseInt(process.env[environmentName] ?? fallback, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`${environmentName} must be a valid TCP port`);
  }
  return port;
}

const port = readPort("VBOT_E2E_PORT", "8437");
const providerPort = readPort("VBOT_E2E_PROVIDER_PORT", "8438");

if (port === providerPort) {
  throw new Error("VBOT_E2E_PORT and VBOT_E2E_PROVIDER_PORT must be different");
}

export const environment = Object.freeze({
  dataDir: path.join(e2eRoot, ".data"),
  e2eRoot,
  fakeProviderEntry: path.join(e2eRoot, "fake-provider.js"),
  fakeProviderHost: "127.0.0.1",
  fakeProviderLogFile: path.join(e2eRoot, ".fake-provider.log"),
  fakeProviderPidFile: path.join(e2eRoot, ".fake-provider.pid"),
  host: "127.0.0.1",
  port,
  providerPort,
  python:
    process.env.VBOT_E2E_PYTHON ??
    (process.platform === "win32" ? "python" : "python3"),
  repoRoot: path.resolve(e2eRoot, "..", ".."),
  resourcesDir: path.join(e2eRoot, ".resources"),
});
