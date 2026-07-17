import { defineConfig, devices } from "@playwright/test";

import { environment } from "./environment.js";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./artifacts/test-results",
  globalSetup: "./server-lifecycle.js",
  globalTeardown: "./server-teardown.js",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: {
    timeout: 7_500,
  },
  reporter: [
    ["line"],
    ["html", { open: "never", outputFolder: "./artifacts/report" }],
  ],
  use: {
    baseURL: `http://${environment.host}:${environment.port}`,
    headless: true,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
