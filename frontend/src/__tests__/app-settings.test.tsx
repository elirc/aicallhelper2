import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

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

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  await waitForSettings();
}

async function openSettings() {
  fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  await screen.findByText("Deepgram API key");
}

describe("settings view", () => {
  it("opens from the gear and focuses the heading", async () => {
    await renderApp();
    await openSettings();
    expect(screen.getByRole("heading", { name: "Settings" })).toHaveFocus();
  });

  it("builds the provider select from the registry, not hard-coded options", async () => {
    await renderApp();
    await openSettings();
    const select = screen.getByLabelText("Answer provider");
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toEqual([
      "Claude Haiku 4.5 (recommended)",
      "Groq GPT-OSS 120B (fastest)",
    ]);
  });

  it("key fields are password inputs; saved keys show the replace placeholder", async () => {
    await renderApp();
    await openSettings();
    const deepgram = screen.getByLabelText("Deepgram API key");
    expect(deepgram).toHaveAttribute("type", "password");
    expect(deepgram).toHaveAttribute("placeholder", "saved — type to replace");
    expect(deepgram).toHaveValue(""); // value is always empty
    const groq = screen.getByLabelText(/Groq GPT-OSS 120B .* API key \(only for the Groq preset\)/);
    expect(groq).toHaveAttribute("placeholder", "");
  });

  it("save sends the whole form but ONLY the key fields actually typed into", async () => {
    await renderApp();
    await openSettings();
    fireEvent.change(screen.getByLabelText("Resume"), {
      target: { value: "my resume" },
    });
    fireEvent.change(
      screen.getByLabelText(/only for the Groq preset/),
      { target: { value: "gsk-123" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(api.set_settings).toHaveBeenCalledTimes(1);
    const patch = api.set_settings.mock.calls[0]?.[0];
    // Profile text travels inside the profiles list; the edited profile is
    // the one that becomes active.
    expect(patch.profiles).toHaveLength(1);
    expect(patch.profiles[0].resume).toBe("my resume");
    expect(patch.profiles[0].id).toBe("default");
    expect(patch.activeProfileId).toBe("default");
    expect(patch.resume).toBeUndefined();
    expect(patch.keys).toEqual({ groq: "gsk-123" }); // deepgram/anthropic untouched
    await screen.findByText("Saved ✓");
  });

  it("save without touching keys omits the keys field entirely", async () => {
    await renderApp();
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    const patch = api.set_settings.mock.calls[0]?.[0];
    expect(patch.keys).toBeUndefined();
  });

  it("Escape closes settings and returns focus to the gear", async () => {
    await renderApp();
    await openSettings();
    fireEvent.keyDown(window, { key: "Escape" });
    await screen.findByText(/Ready — press Record/);
    expect(screen.getByRole("button", { name: "Settings" })).toHaveFocus();
  });

  it("save failures surface in the settings-local error box", async () => {
    api.set_settings.mockResolvedValueOnce({
      ok: false,
      error: { code: "internal", message: "Resume is too long." },
    });
    await renderApp();
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("Resume is too long.");
    expect(screen.getByText("Deepgram API key")).toBeInTheDocument(); // still open
  });
});

describe("style chips", () => {
  it("aria-pressed reflects the PERSISTED style returned by the save", async () => {
    // The save comes back with "balanced" regardless of the click — the UI
    // must render the persisted truth, not the clicked chip.
    api.set_settings.mockResolvedValue(ok(settingsView({ answerStyle: "balanced" })));
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Brief" }));
    await settle();
    expect(api.set_settings).toHaveBeenCalledWith({ answerStyle: "brief" });
    expect(screen.getByRole("button", { name: "Brief" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Balanced" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("persisted flip updates the pressed chip", async () => {
    api.set_settings.mockResolvedValue(ok(settingsView({ answerStyle: "detailed" })));
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Detailed" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Detailed" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
  });
});

describe("hotkey", () => {
  it("shows the chip when registered and includes it in the ready status", async () => {
    await renderApp();
    expect(screen.getByText("Ctrl+Shift+Space")).toBeInTheDocument();
    expect(
      screen.getByText(/Ready — press Record while the other person is speaking or Ctrl\+Shift\+Space/),
    ).toBeInTheDocument();
  });

  it("shows the taken notice when registration failed", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ hotkeyRegistered: false })));
    await renderApp();
    expect(
      screen.getByText(/already taken by another app, so the shortcut is off/),
    ).toBeInTheDocument();
  });

  it("hotkey:toggle does what the Record button does", async () => {
    await renderApp();
    emit("hotkey:toggle", {});
    await settle();
    expect(api.start_session).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Recording call audio…")).toBeInTheDocument();
    emit("hotkey:toggle", {});
    await settle();
    expect(api.stop_session).toHaveBeenCalledWith("s1");
  });

  it("is IGNORED while settings are open", async () => {
    await renderApp();
    await openSettings();
    emit("hotkey:toggle", {});
    await settle();
    expect(api.start_session).not.toHaveBeenCalled();
  });
});

describe("first run", () => {
  it("nudges toward Settings when a key for the selected provider is missing", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ hasAnthropicKey: false })),
    );
    render(<App />);
    expect(
      await screen.findByText(
        "First run: open Settings (gear icon) and add your API keys",
      ),
    ).toBeInTheDocument();
  });

  it("no nudge when the SELECTED provider has its key (other providers may not)", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ hasGroqKey: false })));
    await renderApp();
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
  });
});
