/**
 * Test doubles for the pywebview bridge: a mock js_api installed on window
 * plus a dispatcher for `app:event` CustomEvents — the same seam production
 * uses, driving the REAL components.
 */
import { act } from "@testing-library/react";
import { vi, type Mock } from "vitest";

import type { Result, SettingsView } from "../types";

export function settingsView(overrides: Partial<SettingsView> = {}): SettingsView {
  return {
    resume: "",
    jobDescription: "",
    alwaysOnTop: true,
    llmProvider: "anthropic",
    answerStyle: "balanced",
    hotkey: "Ctrl+Shift+Space",
    providers: [
      { id: "anthropic", displayName: "Claude Haiku 4.5 (recommended)" },
      { id: "groq", displayName: "Groq GPT-OSS 120B (fastest)" },
    ],
    hotkeyRegistered: true,
    hasDeepgramKey: true,
    hasAnthropicKey: true,
    hasGroqKey: false,
    ...overrides,
  };
}

export interface MockApi {
  get_settings: Mock;
  set_settings: Mock;
  start_session: Mock;
  stop_session: Mock;
  ask: Mock;
  cancel_session: Mock;
  heartbeat: Mock;
}

export function ok<T>(value: T): Result<T> {
  return { ok: true, value };
}

export function err(code: string, message: string): Result<never> {
  return { ok: false, error: { code: code as never, message } };
}

export function installMockApi(overrides: Partial<MockApi> = {}): MockApi {
  const api: MockApi = {
    get_settings: vi.fn(async () => ok(settingsView())),
    set_settings: vi.fn(async () => ok(settingsView())),
    start_session: vi.fn(async () => ok("s1")),
    stop_session: vi.fn(async () => ok(null)),
    ask: vi.fn(async () => ok("s1")),
    cancel_session: vi.fn(async () => ok(null)),
    heartbeat: vi.fn(async () => ok(null)),
    ...overrides,
  };
  (window as unknown as { pywebview: { api: MockApi } }).pywebview = { api };
  return api;
}

export function emit(name: string, payload: Record<string, unknown>): void {
  act(() => {
    window.dispatchEvent(
      new CustomEvent("app:event", { detail: { name, payload } }),
    );
  });
}

/** Let microtasks (bridge promise chains) settle inside act. */
export async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}
