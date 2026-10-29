import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The Python package serves the built UI from src/agentwatch/static.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../src/agentwatch/static", emptyOutDir: true },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:4318",
      "/v1": "http://127.0.0.1:4318",
    },
  },
  test: { include: ["src/**/*.test.ts"] },
});
