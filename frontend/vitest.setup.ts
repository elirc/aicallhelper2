import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// Several assertions wait on requestAnimationFrame-driven paints (the answer
// panel coalesces deltas per frame). rAF is a ~16 ms timer in jsdom and can be
// starved when eleven suites run in parallel on a slow machine, so the 1 s
// default occasionally expired on work that was merely late, not wrong.
configure({ asyncUtilTimeout: 5000 });

// With `globals: false`, Testing Library cannot self-register its cleanup —
// without this, each test renders another <App/> on top of the last.
afterEach(() => {
  cleanup();
});
