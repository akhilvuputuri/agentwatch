import { defineConfig, devices } from "@playwright/test";

// Serves the built UI from the Python package with the synthetic demo dataset.
// Run `npm run build` first so src/agentwatch/static is current.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:4399" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run agentwatch serve --demo --port 4399",
      cwd: "..",
      url: "http://127.0.0.1:4399/healthz",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      // Signing in on, for e2e/auth.spec.ts; plain HTTP on loopback, so cookies without Secure.
      command:
        "AGENTWATCH_PASSWORD_HASH=\"$(echo 'e2e owner password' | uv run agentwatch hash-password)\" " +
        "uv run agentwatch serve --demo --port 4398 --auth-user owner --insecure-cookies",
      cwd: "..",
      url: "http://127.0.0.1:4398/healthz",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
