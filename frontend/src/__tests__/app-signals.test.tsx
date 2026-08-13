import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import {
  emit,
  installMockApi,
  ok,
  settingsView,
  settle,
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

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  // The status line renders before settings arrive; wait for the real thing.
  await waitForSettings();
}

async function startRecording() {
  fireEvent.click(screen.getByRole("button", { name: "Record" }));
  await settle();
}

const SILENCE_HINT = /No call audio detected yet/;

describe("silent-capture hint", () => {
  // Loopback capture that produces nothing — audio routed to a headset, the
  // wrong output device, a muted call — is the single most common real-world
  // failure, and the user finds out only at Stop ("No speech detected") after
  // wasting the question. The hint surfaces it while there is still time.
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it("appears after sustained silence while recording", async () => {
    await renderApp();
    await startRecording();
    expect(screen.queryByText(SILENCE_HINT)).toBeNull();
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.getByText(SILENCE_HINT)).toBeInTheDocument();
  });

  it("does not fire on a short pause", async () => {
    await renderApp();
    await startRecording();
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.queryByText(SILENCE_HINT)).toBeNull();
  });

  it("never appears once any audible frame arrived", async () => {
    await renderApp();
    await startRecording();
    emit("audio:level", { sessionId: "s1", rms: 0.2 });
    act(() => {
      vi.advanceTimersByTime(9000);
    });
    expect(screen.queryByText(SILENCE_HINT)).toBeNull();
  });

  it("digital silence does not count as audio", async () => {
    await renderApp();
    await startRecording();
    for (let i = 0; i < 40; i += 1) {
      emit("audio:level", { sessionId: "s1", rms: 0 });
    }
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.getByText(SILENCE_HINT)).toBeInTheDocument();
  });

  it("clears the moment audio starts flowing", async () => {
    await renderApp();
    await startRecording();
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.getByText(SILENCE_HINT)).toBeInTheDocument();
    emit("audio:level", { sessionId: "s1", rms: 0.05 });
    expect(screen.queryByText(SILENCE_HINT)).toBeNull();
  });

  it("is gone after the recording stops", async () => {
    await renderApp();
    await startRecording();
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.getByText(SILENCE_HINT)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(screen.queryByText(SILENCE_HINT)).toBeNull();
  });

  it("resets between recordings", async () => {
    await renderApp();
    await startRecording();
    emit("audio:level", { sessionId: "s1", rms: 0.2 }); // this one had audio
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:done", {
      sessionId: "s1",
      transcript: "q",
      answer: "a",
      metrics: { sttFinalizeMs: 1, firstTokenMs: 2, totalMs: 3 },
    });
    await settle();
    api.start_session.mockResolvedValueOnce(ok("s2"));
    await startRecording();
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    // The new recording is silent, so the hint must return.
    expect(screen.getByText(SILENCE_HINT)).toBeInTheDocument();
  });
});

describe("hotkey status messaging", () => {
  it("blames another app only when the shortcut is well-formed but taken", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ hotkeyRegistered: false, hotkeyStatus: "unavailable" })),
    );
    await renderApp();
    expect(
      screen.getByText(/already taken by another app, so the shortcut is off/),
    ).toBeInTheDocument();
  });

  it("says the shortcut is invalid when Windows cannot parse it", async () => {
    // Sending someone to hunt for a conflicting app when they simply typed
    // "Ctrl+Foo" wastes their time.
    api.get_settings.mockResolvedValue(
      ok(settingsView({ hotkey: "Ctrl+Foo", hotkeyRegistered: false, hotkeyStatus: "invalid" })),
    );
    await renderApp();
    expect(
      screen.getByText(/isn't a shortcut Windows understands/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/already taken by another app/)).toBeNull();
  });

  it("says nothing when the user deliberately disabled the shortcut", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ hotkey: "", hotkeyRegistered: false, hotkeyStatus: "disabled" })),
    );
    await renderApp();
    expect(screen.queryByText(/shortcut is off/)).toBeNull();
    expect(screen.queryByText(/isn't a shortcut Windows understands/)).toBeNull();
  });

  it("shows the chip and no notice when registered", async () => {
    await renderApp();
    expect(screen.getByText("Ctrl+Shift+Space")).toBeInTheDocument();
    expect(screen.queryByText(/shortcut is off/)).toBeNull();
  });
});

describe("content-protection warning", () => {
  const WARNING = /may be visible if you share your screen/;

  it("is silent while protection holds", async () => {
    await renderApp();
    expect(screen.queryByText(WARNING)).toBeNull();
  });

  it("warns loudly when Windows refused to hide the window", async () => {
    // Believing you are hidden while being broadcast is this app's worst
    // possible failure, so it is an alert, not a log line.
    await renderApp();
    emit("protection:failed", {});
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(WARNING);
  });

  it("clears when a later attempt succeeds", async () => {
    await renderApp();
    emit("protection:failed", {});
    expect(screen.getByText(WARNING)).toBeInTheDocument();
    emit("protection:ok", {});
    expect(screen.queryByText(WARNING)).toBeNull();
  });

  it("survives a recording cycle", async () => {
    await renderApp();
    emit("protection:failed", {});
    await startRecording();
    expect(screen.getByText(WARNING)).toBeInTheDocument();
  });
});

describe("level meter accessibility", () => {
  it("is hidden from assistive tech — the textual hint carries the meaning", async () => {
    // A meter that changes eight times a second is pure noise in a screen
    // reader; the silence hint says the same thing in words.
    await renderApp();
    await startRecording();
    expect(screen.getByTestId("level-meter")).toHaveAttribute("aria-hidden", "true");
  });
});
