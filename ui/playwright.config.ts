import { defineConfig, devices } from "@playwright/test";

// Serves the built UI from the Python package with the synthetic demo dataset.
// Run `npm run build` first so src/agentwatch/static is current.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:4399" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "uv run agentwatch serve --demo --port 4399",
    cwd: "..",
    url: "http://127.0.0.1:4399/healthz",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
