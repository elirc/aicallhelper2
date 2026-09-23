/**
 * R01 / R11: the capture-protection verdict is tri-state, visible in EVERY
 * view, adopted from the core's get_status() snapshot on page load, and
 * ordered by a monotonic revision so older news never overwrites newer.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { parseStatus } from "../bridge";
import {
  deferred,
  emit,
  err,
  installMockApi,
  ok,
  settingsView,
  settle,
  statusSnapshot,
  waitForSettings,
  type MockApi,
} from "./testutils";

let api: MockApi;

beforeEach(() => {
  api = installMockApi();
});

afterEach(() => {
  vi.useRealTimers();
});

const WARNING = /may be visible if you share your screen/;
const UNKNOWN = "Screen-share protection not confirmed yet";
const PROTECTED = "Hidden from screen capture";

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  await waitForSettings();
}

async function openSettings() {
  fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  await screen.findByText("Deepgram API key");
}

describe("protection verdict in every view", () => {
  it("stays an alert after opening Settings, and Settings claims nothing false", async () => {
    await renderApp();
    emit("protection:failed", {});
    await openSettings();
    expect(screen.getByRole("alert")).toHaveTextContent(WARNING);
    expect(screen.queryByText(/hidden from screen sharing/i)).toBeNull();
  });

  it("shows 'not confirmed' before any verdict, in the full view and Settings", async () => {
    await renderApp();
    expect(screen.getByText(UNKNOWN)).toBeInTheDocument();
    expect(screen.queryByText(PROTECTED)).toBeNull();
    await openSettings();
    expect(screen.getByText(UNKNOWN)).toBeInTheDocument();
  });

  it("shows the confirmed verdict once Windows reports it", async () => {
    await renderApp();
    emit("protection:ok", {});
    expect(screen.getByText(PROTECTED)).toBeInTheDocument();
    expect(screen.queryByText(UNKNOWN)).toBeNull();
  });

  it("shows 'not confirmed' in the prompter strip too", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
    render(<App />);
    await screen.findByLabelText("Exit prompter");
    expect(screen.getByText(UNKNOWN)).toBeInTheDocument();
  });

  it("Settings opened from the prompter keeps the failure alert", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
    render(<App />);
    await screen.findByLabelText("Exit prompter");
    emit("protection:failed", {});
    // No gear in the strip; the Settings view is state-driven, so open it
    // through the same action the full view uses via a fresh full-layout
    // render is not possible here — assert the strip alert instead, then
    // switch to full and open Settings.
    expect(screen.getByRole("alert")).toHaveTextContent(/Not hidden from screen share/);
    api.set_settings.mockResolvedValue(ok(settingsView({ layoutMode: "full" })));
    fireEvent.click(screen.getByLabelText("Exit prompter"));
    await screen.findByRole("button", { name: "Settings" });
    await openSettings();
    expect(screen.getByRole("alert")).toHaveTextContent(WARNING);
  });
});

describe("get_status snapshot", () => {
  it("adopts a failed verdict on load with no push event at all", async () => {
    // The one-time event can fire before React subscribes (or before a
    // reloaded page exists); the snapshot is what makes it survive.
    api.get_status = vi.fn(async () => ok(statusSnapshot({ protection: "unprotected" })));
    await renderApp();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(WARNING));
    expect(api.get_status).toHaveBeenCalledTimes(1);
  });

  it("an older snapshot cannot overwrite a fresher pushed verdict", async () => {
    const pending = deferred();
    api.get_status = vi.fn(() => pending.promise);
    await renderApp();
    emit("protection:ok", { revision: 5 });
    pending.resolve(ok(statusSnapshot({ revision: 4, protection: "unprotected" })));
    await settle();
    expect(screen.queryByText(WARNING)).toBeNull();
    expect(screen.getByText(PROTECTED)).toBeInTheDocument();
  });

  it("a stale protection event is ignored once a newer revision is known", async () => {
    await renderApp();
    emit("protection:failed", { revision: 7 });
    emit("protection:ok", { revision: 6 }); // late delivery of older news
    expect(screen.getByRole("alert")).toHaveTextContent(WARNING);
  });

  it("a failed or missing status command leaves the verdict unknown", async () => {
    api.get_status = vi.fn(async () => err("internal", "boom"));
    await renderApp();
    await settle();
    expect(screen.getByText(UNKNOWN)).toBeInTheDocument();
  });

  it("re-adopts a session still recording behind a reloaded page, so Stop works", async () => {
    api.get_status = vi.fn(async () =>
      ok(statusSnapshot({ session: { id: "s9", phase: "recording" } })),
    );
    render(<App />);
    const stop = await screen.findByRole("button", { name: "Stop & Answer" });
    emit("stt:partial", { sessionId: "s9", text: "still heard", isFinal: false });
    expect(screen.getByText("still heard")).toBeInTheDocument();
    fireEvent.click(stop);
    await settle();
    expect(api.stop_session).toHaveBeenCalledWith("s9");
    expect(api.start_session).not.toHaveBeenCalled(); // no second session
  });

  it("does not resume an idle or unknown-phase session", async () => {
    api.get_status = vi.fn(async () =>
      ok(statusSnapshot({ session: { id: "s9", phase: "idle" } })),
    );
    await renderApp();
    await settle();
    expect(screen.getByRole("button", { name: "Record" })).toBeInTheDocument();
  });
});

describe("event page generation (R07)", () => {
  it("drops events stamped for a superseded page generation", async () => {
    api.get_status = vi.fn(async () => ok(statusSnapshot({ pageGeneration: 3 })));
    await renderApp();
    await waitFor(() => expect(api.get_status).toHaveBeenCalled());
    await settle();
    // A timed-out evaluate_js from page 2 lands on page 3.
    emit("protection:failed", { pageGen: 2 });
    expect(screen.queryByText(WARNING)).toBeNull();
    emit("protection:failed", { pageGen: 3 });
    expect(screen.getByRole("alert")).toHaveTextContent(WARNING);
  });
});

describe("parseStatus", () => {
  it("accepts the Result envelope and a bare object", () => {
    const snap = statusSnapshot({ revision: 2 });
    expect(parseStatus(ok(snap))).toMatchObject({ ok: true, value: { revision: 2 } });
    expect(parseStatus(snap)).toMatchObject({ ok: true, value: { revision: 2 } });
  });

  it("rejects malformed snapshots instead of trusting them", () => {
    expect(parseStatus(statusSnapshot({ protection: "maybe" })).ok).toBe(false);
    expect(parseStatus(statusSnapshot({ revision: "1" })).ok).toBe(false);
    expect(parseStatus(null).ok).toBe(false);
    expect(parseStatus(err("internal", "x")).ok).toBe(false);
  });

  it("normalizes a missing session to idle", () => {
    const parsed = parseStatus({ revision: 1, protection: "unknown" });
    expect(parsed).toMatchObject({ ok: true, value: { session: { id: null, phase: "idle" } } });
  });
});

describe("settings load failure (R11)", () => {
  it("says so and retries until the core answers", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.get_settings
      .mockResolvedValueOnce(err("internal", "The app is still starting."))
      .mockResolvedValue(ok(settingsView()));
    render(<App />);
    await screen.findByText(/Settings could not be loaded/);
    await act(async () => {
      vi.advanceTimersByTime(3100);
    });
    await waitForSettings();
    expect(screen.queryByText(/Settings could not be loaded/)).toBeNull();
    expect(api.get_settings).toHaveBeenCalledTimes(2);
  });
});
