/**
 * R09: a Settings draft is discarded only by an explicit Discard, saves are
 * serialized with a visible saving state, and key edits follow the user's
 * latest intent.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import {
  deferred,
  err,
  installMockApi,
  ok,
  profile,
  settingsView,
  settle,
  waitForSettings,
  type MockApi,
} from "./testutils";

let api: MockApi;

beforeEach(() => {
  api = installMockApi();
});

async function openSettings() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  await waitForSettings();
  fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  await screen.findByText("Deepgram API key");
}

describe("discarding a draft needs an explicit decision", () => {
  it("repeated Escape keeps the draft and the Settings view", async () => {
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "edited" } });
    fireEvent.keyDown(window, { key: "Escape" });
    await screen.findByText("You have unsaved changes.");
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.keyDown(window, { key: "Escape" });
    await settle();
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByLabelText("Focus")).toHaveValue("edited");
  });

  it("repeated Back keeps the draft too", async () => {
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "edited" } });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await settle();
    expect(screen.getByLabelText("Focus")).toHaveValue("edited");
  });

  it("'Save and go back' saves, then leaves", async () => {
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "edited" } });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save and go back" }));
    await screen.findByText(/Ready — press Record/);
    expect(api.set_settings.mock.calls[0]?.[0].profiles[0].focus).toBe("edited");
  });

  it("'Save and go back' stays put, draft intact, when the save fails", async () => {
    api.set_settings.mockResolvedValueOnce(err("internal", "Disk full."));
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "edited" } });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save and go back" }));
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("Disk full.");
    expect(screen.getByLabelText("Focus")).toHaveValue("edited");
  });
});

describe("saving state", () => {
  it("serializes saves and makes the form read-only while one is in flight", async () => {
    const pending = deferred();
    api.set_settings.mockReturnValueOnce(pending.promise);
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "A" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    const savingButton = screen.getByRole("button", { name: "Saving…" });
    expect(savingButton).toBeDisabled();
    // Edits cannot be typed into a draft the response would then overwrite.
    expect(screen.getByLabelText("Focus")).toBeDisabled();
    fireEvent.click(savingButton);
    await settle();
    expect(api.set_settings).toHaveBeenCalledTimes(1);
    pending.resolve(ok(settingsView({ profiles: [profile({ focus: "A" })] })));
    await screen.findByText("Saved ✓");
    expect(screen.getByLabelText("Focus")).toBeEnabled();
    expect(screen.getByLabelText("Focus")).toHaveValue("A");
  });
});

describe("key removal versus a retyped key", () => {
  it("a key typed after Remove wins over the queued removal", async () => {
    await openSettings();
    fireEvent.click(screen.getByLabelText("Remove Deepgram API key"));
    fireEvent.change(screen.getByLabelText("Deepgram API key"), {
      target: { value: "dg-new" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(api.set_settings.mock.calls[0]?.[0].keys).toEqual({ deepgram: "dg-new" });
  });

  it("Undo remove cancels a queued removal", async () => {
    await openSettings();
    fireEvent.click(screen.getByLabelText("Remove Deepgram API key"));
    fireEvent.click(screen.getByRole("button", { name: "Keep Deepgram API key" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    expect(api.set_settings.mock.calls[0]?.[0].keys).toBeUndefined();
  });
});

describe("profile limits", () => {
  it("Add and Duplicate are disabled at the core's 20-profile limit", async () => {
    const twenty = Array.from({ length: 20 }, (_, i) =>
      profile({ id: i === 0 ? "default" : `p${i}`, name: `P${i}` }),
    );
    api.get_settings.mockResolvedValue(ok(settingsView({ profiles: twenty })));
    await openSettings();
    expect(screen.getByRole("button", { name: "Add profile" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Duplicate" })).toBeDisabled();
  });

  it("Duplicate keeps the copy's name within the 60-character limit", async () => {
    const long = "N".repeat(60);
    api.get_settings.mockResolvedValue(ok(settingsView({ profiles: [profile({ name: long })] })));
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Duplicate" }));
    const name = (screen.getByLabelText("Profile name") as HTMLInputElement).value;
    expect(name.length).toBe(60);
    expect(name.endsWith(" (copy)")).toBe(true);
  });
});
