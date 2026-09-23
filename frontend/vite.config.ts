import { readFileSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The app version, from package.json (which tools/release_meta.py keeps equal
// to pyproject.toml). Components read the compile-time constant
// __APP_VERSION__ instead of hard-coding a version string.
const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8")) as {
  version: string;
};

export default defineConfig({
  plugins: [react()],
  base: "./",
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
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
