import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// With `globals: false`, Testing Library cannot self-register its cleanup —
// without this, each test renders another <App/> on top of the last.
afterEach(() => {
  cleanup();
});
