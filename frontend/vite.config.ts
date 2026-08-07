import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", target: "es2022" },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    globals: false,
    // This machine is slow; jsdom suites need generous timeouts.
    testTimeout: 30000,
    hookTimeout: 30000,
  },
});
