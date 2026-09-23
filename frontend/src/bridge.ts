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
  StatusSnapshot,
} from "./types";

interface RawApi {
  get_settings(): Promise<Result<SettingsView>>;
  set_settings(patch: SettingsPatch): Promise<Result<SettingsView>>;
  start_session(): Promise<Result<string>>;
  stop_session(sessionId: string): Promise<Result<null>>;
  ask(text: string): Promise<Result<string>>;
  cancel_session(sessionId: string): Promise<Result<null>>;
  heartbeat(): Promise<Result<null>>;
  dock_window(): Promise<Result<null>>;
  open_external(url: string): Promise<Result<null>>;
  /** Optional (newer cores): block one native close while a draft is unsaved. */
  set_close_guard?(active: boolean): Promise<Result<null>>;
  /** Optional: an older core has no status command. */
  get_status?(): Promise<Result<StatusSnapshot> | StatusSnapshot>;
}

declare global {
  interface Window {
    pywebview?: { api: RawApi };
  }
}

/** How long one command waits for the bridge before answering OFFLINE. The
 *  shared readiness wait itself continues, so a late bridge still serves
 *  later commands. */
export const BRIDGE_READY_TIMEOUT_MS = 30_000;

let readyWait: Promise<RawApi> | null = null;

/** ONE shared listener, not one per call: the 3 s heartbeat would otherwise
 *  pile up a never-firing listener every tick while the bridge is absent. */
function sharedReady(): Promise<RawApi> {
  if (!readyWait) {
    readyWait = new Promise((resolve) => {
      window.addEventListener(
        "pywebviewready",
        () => {
          readyWait = null;
          resolve(window.pywebview!.api);
        },
        { once: true },
      );
    });
  }
  return readyWait;
}

function whenReady(): Promise<RawApi> {
  if (window.pywebview?.api) return Promise.resolve(window.pywebview.api);
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(
      () => reject(new Error("bridge not ready")),
      BRIDGE_READY_TIMEOUT_MS,
    );
    void sharedReady().then((api) => {
      window.clearTimeout(timer);
      resolve(api);
    });
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

const NO_STATUS: Result<never> = {
  ok: false,
  error: { code: "internal", message: "The app core has no status command." },
};

const VERDICTS = new Set(["protected", "unprotected", "unknown"]);

/** Validate a status snapshot from the core. Accepts the Result envelope or a
 *  bare object; anything malformed is treated as "no snapshot" rather than
 *  trusted — a bad snapshot must never flip the protection verdict. */
export function parseStatus(raw: unknown): Result<StatusSnapshot> {
  let value: unknown = raw;
  if (raw && typeof raw === "object" && "ok" in raw) {
    const env = raw as Result<unknown>;
    if (!env.ok) return env;
    value = env.value;
  }
  if (!value || typeof value !== "object") return NO_STATUS;
  const v = value as Record<string, unknown>;
  const session = v.session as Record<string, unknown> | undefined;
  if (
    typeof v.revision !== "number" ||
    typeof v.protection !== "string" ||
    !VERDICTS.has(v.protection)
  ) {
    return NO_STATUS;
  }
  return {
    ok: true,
    value: {
      revision: v.revision,
      coreReady: v.coreReady === true,
      protection: v.protection as StatusSnapshot["protection"],
      session: {
        id: session && typeof session.id === "string" ? session.id : null,
        phase: session && typeof session.phase === "string" ? session.phase : "idle",
      },
      ...(typeof v.pageGeneration === "number" ? { pageGeneration: v.pageGeneration } : {}),
      ...(v.core === "starting" || v.core === "ready" || v.core === "failed"
        ? { core: v.core, coreError: typeof v.coreError === "string" ? v.coreError : null }
        : {}),
    },
  };
}

export const bridge = {
  getSettings: () => call((api) => api.get_settings()),
  /** Authoritative readiness/protection/session snapshot (R01/R08/R11). */
  getStatus: async (): Promise<Result<StatusSnapshot>> => {
    try {
      const api = await whenReady();
      if (typeof api.get_status !== "function") return NO_STATUS;
      return parseStatus(await api.get_status());
    } catch {
      return OFFLINE;
    }
  },
  /** Tell the shell whether closing the window would lose a Settings draft.
   *  A core without the command simply never guards (the old behaviour). */
  setCloseGuard: (active: boolean): Promise<Result<null>> =>
    call((api) =>
      typeof api.set_close_guard === "function"
        ? api.set_close_guard(active)
        : Promise.resolve<Result<null>>({ ok: true, value: null }),
    ),
  /** Synchronous: whether the connected core can answer getStatus at all.
   *  Callers use it to keep a defined, immediate fallback when it cannot. */
  hasStatus: (): boolean => typeof window.pywebview?.api?.get_status === "function",
  setSettings: (patch: SettingsPatch) => call((api) => api.set_settings(patch)),
  startSession: () => call((api) => api.start_session()),
  stopSession: (sessionId: string) => call((api) => api.stop_session(sessionId)),
  ask: (text: string) => call((api) => api.ask(text)),
  cancelSession: (sessionId: string) => call((api) => api.cancel_session(sessionId)),
  heartbeat: () => call((api) => api.heartbeat()),
  /** Move the window to the top-centre of its display — the camera line. */
  dockWindow: () => call((api) => api.dock_window()),
  /** Open an https link in the system browser (the webview never navigates). */
  openExternal: (url: string) => call((api) => api.open_external(url)),
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
