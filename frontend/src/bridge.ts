/**
 * The frontend's only doorway to the core: promise-returning commands on the
 * pywebview js_api, and the `app:event` CustomEvent stream. Every command
 * resolves a Result envelope — nothing throws across the boundary.
 */
import type {
  AppEventDetail,
  Result,
  SettingsPatch,
  SettingsView,
} from "./types";

interface RawApi {
  get_settings(): Promise<Result<SettingsView>>;
  set_settings(patch: SettingsPatch): Promise<Result<SettingsView>>;
  start_session(): Promise<Result<string>>;
  stop_session(sessionId: string): Promise<Result<null>>;
  ask(text: string): Promise<Result<string>>;
  cancel_session(sessionId: string): Promise<Result<null>>;
  heartbeat(): Promise<Result<null>>;
}

declare global {
  interface Window {
    pywebview?: { api: RawApi };
  }
}

function whenReady(): Promise<RawApi> {
  if (window.pywebview?.api) return Promise.resolve(window.pywebview.api);
  return new Promise((resolve) => {
    window.addEventListener(
      "pywebviewready",
      () => resolve(window.pywebview!.api),
      { once: true },
    );
  });
}

const OFFLINE: Result<never> = {
  ok: false,
  error: { code: "internal", message: "The app core is not reachable." },
};

async function call<T>(fn: (api: RawApi) => Promise<Result<T>>): Promise<Result<T>> {
  try {
    const api = await whenReady();
    const result = await fn(api);
    return result ?? OFFLINE;
  } catch {
    return OFFLINE;
  }
}

export const bridge = {
  getSettings: () => call((api) => api.get_settings()),
  setSettings: (patch: SettingsPatch) => call((api) => api.set_settings(patch)),
  startSession: () => call((api) => api.start_session()),
  stopSession: (sessionId: string) => call((api) => api.stop_session(sessionId)),
  ask: (text: string) => call((api) => api.ask(text)),
  cancelSession: (sessionId: string) => call((api) => api.cancel_session(sessionId)),
  heartbeat: () => call((api) => api.heartbeat()),
};

/** One listener fans events out by name; returns an unsubscribe. */
export function subscribeAppEvents(
  handler: (detail: AppEventDetail) => void,
): () => void {
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<AppEventDetail>).detail;
    if (detail && typeof detail.name === "string" && detail.payload) {
      handler(detail);
    }
  };
  window.addEventListener("app:event", listener);
  return () => window.removeEventListener("app:event", listener);
}

/** Renderer liveness ping — the core's crash-recovery fallback signal. */
export function startHeartbeat(intervalMs = 3000): () => void {
  const id = window.setInterval(() => {
    void bridge.heartbeat();
  }, intervalMs);
  return () => window.clearInterval(id);
}
