/**
 * Frontend side of the shell/release contract (CONTRACT §8–12): save base
 * revisions, key-storage and settings-file notices, the native close guard,
 * core readiness, and the single-source version chip.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import {
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

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record|First run/);
  await waitForSettings();
}

async function openSettings() {
  await renderApp();
  fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  await screen.findByText("Deepgram API key");
}

describe("version chip", () => {
  it("comes from the single version source, not a hard-coded string", async () => {
    await openSettings();
    expect(screen.getByText(`v${__APP_VERSION__}`)).toBeInTheDocument();
  });
});

describe("settings save base revision", () => {
  it("sends the revision the draft was built on, then advances it", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ settingsRevision: 7 })));
    api.set_settings.mockResolvedValueOnce(ok(settingsView({ settingsRevision: 8 })));
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved ✓");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(api.set_settings.mock.calls[0]?.[0].baseRevision).toBe(7);
    expect(api.set_settings.mock.calls[1]?.[0].baseRevision).toBe(8);
  });

  it("a stale-revision rejection reloads settings, says so, and keeps the draft", async () => {
    api.get_settings
      .mockResolvedValueOnce(ok(settingsView({ settingsRevision: 3 })))
      .mockResolvedValue(ok(settingsView({ settingsRevision: 5 })));
    api.set_settings.mockResolvedValueOnce(
      err("internal", "Settings changed since this page loaded (a newer save). Nothing was applied."),
    );
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "my edit" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/changed by a newer save/),
    );
    expect(api.get_settings).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Focus")).toHaveValue("my edit");
    // Saving again is an informed choice, built on the fresh revision.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(api.set_settings.mock.calls[1]?.[0].baseRevision).toBe(5);
    expect(api.set_settings.mock.calls[1]?.[0].profiles[0].focus).toBe("my edit");
  });

  it("an older core without settingsRevision gets no baseRevision", async () => {
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(api.set_settings.mock.calls[0]?.[0]).not.toHaveProperty("baseRevision");
  });
});

describe("storage notices", () => {
  it("warns next to a key stored without encryption", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ keyStorage: { deepgram: "plaintext", anthropic: "encrypted" } })),
    );
    await openSettings();
    expect(screen.getAllByText(/stored WITHOUT encryption/)).toHaveLength(1);
  });

  it("says when the settings file could not be read, naming the backup", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ settingsFile: { load: "invalid", backup: "settings.json.bak-1" } })),
    );
    await renderApp();
    expect(screen.getByText(/settings file couldn't be read/)).toHaveTextContent(
      "A copy was saved as settings.json.bak-1.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByText("Deepgram API key");
    expect(screen.getByText(/settings file couldn't be read/)).toBeInTheDocument();
  });

  it("is silent for a normal or first-run file", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ settingsFile: { load: "missing", backup: null } })),
    );
    await renderApp();
    expect(screen.queryByText(/couldn't be read/)).toBeNull();
  });
});

describe("native close guard", () => {
  it("guards while the draft is dirty, prompts on a cancelled close, releases on Discard", async () => {
    api.set_close_guard = vi.fn(async () => ok(null));
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "unsaved" } });
    await waitFor(() => expect(api.set_close_guard).toHaveBeenLastCalledWith(true));
    emit("window:close-requested", {});
    await screen.findByText("You have unsaved changes.");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await screen.findByText(/Ready — press Record/);
    expect(api.set_close_guard).toHaveBeenLastCalledWith(false);
  });
});

describe("core readiness", () => {
  it("core:failed shows a persistent alert; core:ready clears it", async () => {
    await renderApp();
    emit("core:failed", { revision: 4, error: "ImportError: sounddevice" });
    expect(screen.getByRole("alert")).toHaveTextContent(/failed to start \(ImportError: sounddevice\)/);
    emit("core:ready", { revision: 5 });
    expect(screen.queryByText(/failed to start/)).toBeNull();
  });

  it("an older core event cannot undo a newer one", async () => {
    await renderApp();
    emit("core:failed", { revision: 6, error: "boom" });
    emit("core:ready", { revision: 2 });
    expect(screen.getByText(/failed to start/)).toBeInTheDocument();
  });

  it("a failed core from the load snapshot shows the alert in the prompter too", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
    api.get_status = vi.fn(async () =>
      ok(statusSnapshot({ core: "failed", coreError: "no audio backend" })),
    );
    render(<App />);
    await screen.findByLabelText("Exit prompter");
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/no audio backend/),
    );
  });
});
