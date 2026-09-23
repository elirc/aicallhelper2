import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import {
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

const TWO_PROFILES = [
  profile(),
  profile({ id: "sales", name: "Acme renewal", callType: "sales", resume: "bg" }),
];

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  await waitForSettings();
}

async function openSettings() {
  fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  await screen.findByText("Deepgram API key");
}

describe("profile and call-type quick switcher", () => {
  it("hides the profile select with one profile and shows it with two", async () => {
    await renderApp();
    expect(screen.queryByLabelText("Profile")).toBeNull();
    expect(screen.getByLabelText("Call type")).toHaveValue("behavioral");
  });

  it("switching the profile persists activeProfileId and renders the PERSISTED value", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ profiles: TWO_PROFILES, activeProfileId: "default" })),
    );
    // The core answers with the OTHER id: the select must show what landed.
    api.set_settings.mockResolvedValue(
      ok(
        settingsView({
          profiles: TWO_PROFILES,
          activeProfileId: "sales",
          callType: "sales",
          resume: "bg",
        }),
      ),
    );
    await renderApp();
    const select = await screen.findByLabelText("Profile");
    fireEvent.change(select, { target: { value: "sales" } });
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ activeProfileId: "sales" });
    await waitFor(() => expect(screen.getByLabelText("Profile")).toHaveValue("sales"));
    expect(screen.getByLabelText("Call type")).toHaveValue("sales");
    // The header chip names the active profile once there is more than one.
    expect(screen.getByText("Claude Haiku 4.5 · Acme renewal")).toBeInTheDocument();
  });

  it("changing the call type persists it on the active profile", async () => {
    api.set_settings.mockResolvedValue(ok(settingsView({ callType: "technical" })));
    await renderApp();
    fireEvent.change(screen.getByLabelText("Call type"), { target: { value: "technical" } });
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ callType: "technical" });
    await waitFor(() => expect(screen.getByLabelText("Call type")).toHaveValue("technical"));
  });

  it("builds the call-type options from the view, in order", async () => {
    await renderApp();
    const options = Array.from(
      screen.getByLabelText("Call type").querySelectorAll("option"),
    ).map((o) => o.textContent);
    expect(options).toEqual([
      "Behavioral interview",
      "Technical screen",
      "System design",
      "Recruiter screen",
      "Sales or customer call",
      "General meeting",
    ]);
  });
});

describe("profiles in settings", () => {
  it("Add profile creates an entry with a client id and Save activates it", async () => {
    await renderApp();
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Add profile" }));
    fireEvent.change(screen.getByLabelText("Profile name"), {
      target: { value: "Backend screen" },
    });
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "Go, gRPC" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    const patch = api.set_settings.mock.calls[0]?.[0];
    expect(patch.profiles).toHaveLength(2);
    const added = patch.profiles[1];
    expect(added.name).toBe("Backend screen");
    expect(added.focus).toBe("Go, gRPC");
    expect(added.id).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    expect(patch.activeProfileId).toBe(added.id);
  });

  it("Delete is disabled with one profile and enabled with two", async () => {
    await renderApp();
    await openSettings();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Duplicate" }));
    expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled();
    expect(screen.getByLabelText("Profile name")).toHaveValue("Default (copy)");
  });

  it("labels switch to Background and Call context for a sales profile", async () => {
    await renderApp();
    await openSettings();
    expect(screen.getByLabelText("Resume")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Call type"), { target: { value: "sales" } });
    expect(screen.getByLabelText("Background / resume")).toBeInTheDocument();
    expect(screen.getByLabelText("Call context")).toBeInTheDocument();
    expect(screen.queryByLabelText("Job description")).toBeNull();
  });

  it("after Save the panel adopts the core's canonical profile ids", async () => {
    api.set_settings.mockResolvedValue(
      ok(
        settingsView({
          profiles: [profile(), profile({ id: "srv1", name: "New one" })],
          activeProfileId: "srv1",
        }),
      ),
    );
    await renderApp();
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Add profile" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved ✓");
    expect(screen.getByLabelText("Edit profile")).toHaveValue("srv1");
  });

  it("a key field typed into and emptied again does NOT erase the saved key", async () => {
    await renderApp();
    await openSettings();
    const deepgram = screen.getByLabelText("Deepgram API key");
    fireEvent.change(deepgram, { target: { value: "x" } });
    fireEvent.change(deepgram, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    const patch = api.set_settings.mock.calls[0]?.[0];
    expect(patch.keys).toBeUndefined();
  });

  it("Remove sends an explicit empty key for that provider only", async () => {
    await renderApp();
    await openSettings();
    fireEvent.click(screen.getByLabelText("Remove Deepgram API key"));
    expect(screen.getByLabelText("Deepgram API key")).toHaveAttribute(
      "placeholder",
      "will be removed on Save",
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await settle();
    const patch = api.set_settings.mock.calls[0]?.[0];
    expect(patch.keys).toEqual({ deepgram: "" });
  });

  it("Back with unsaved edits asks first; Discard leaves, Keep editing stays", async () => {
    await renderApp();
    await openSettings();
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "edited" } });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await screen.findByText("You have unsaved changes.");
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByText("You have unsaved changes.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await screen.findByText(/Ready — press Record/);
    expect(api.set_settings).not.toHaveBeenCalled();
  });

  it("Get a key links open in the system browser, never in the webview", async () => {
    await renderApp();
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: /Get a key at console.deepgram.com/ }));
    await settle();
    expect(api.open_external).toHaveBeenCalledWith("https://console.deepgram.com");
  });
});
