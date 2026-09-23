/** The bridge's readiness wait: one shared listener and a bounded wait. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BRIDGE_READY_TIMEOUT_MS, bridge } from "../bridge";
import { installMockApi } from "./testutils";

beforeEach(() => {
  Reflect.deleteProperty(window, "pywebview");
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("waiting for pywebview", () => {
  it("many early calls share ONE ready listener, and all resolve once ready", async () => {
    const add = vi.spyOn(window, "addEventListener");
    const calls = [bridge.heartbeat(), bridge.heartbeat(), bridge.heartbeat(), bridge.getSettings()];
    const readyListeners = add.mock.calls.filter(([name]) => name === "pywebviewready");
    expect(readyListeners).toHaveLength(1);
    const api = installMockApi();
    window.dispatchEvent(new Event("pywebviewready"));
    const results = await Promise.all(calls);
    expect(results.every((r) => r.ok)).toBe(true);
    expect(api.heartbeat).toHaveBeenCalledTimes(3);
  });

  it("a command answers OFFLINE instead of hanging when the bridge never appears", async () => {
    vi.useFakeTimers();
    const pending = bridge.startSession();
    await vi.advanceTimersByTimeAsync(BRIDGE_READY_TIMEOUT_MS + 1);
    const result = await pending;
    expect(result.ok).toBe(false);
  });

  it("getStatus reports 'no status command' for an older core", async () => {
    installMockApi();
    const result = await bridge.getStatus();
    expect(result.ok).toBe(false);
    expect(bridge.hasStatus()).toBe(false);
  });
});
