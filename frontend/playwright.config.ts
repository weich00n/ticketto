import { loadEnvConfig } from "@next/env";
import { defineConfig, devices } from "@playwright/test";

// The tests create their Cognito users with the pool settings in .env.local.
loadEnvConfig(process.cwd());

// End-to-end tests run against a deployed copy of the app and the real backend.
// See e2e/README.md for the environment they need.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  // The scenarios share one backend, so they run one after another.
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL:
      process.env.E2E_BASE_URL ?? "https://d3nt9zhb2qmf35.cloudfront.net",
    video: "on",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
