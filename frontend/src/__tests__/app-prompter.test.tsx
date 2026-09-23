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

async function renderPrompter() {
  api.get_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
  api.set_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
  render(<App />);
  await waitForSettings();
  await screen.findByLabelText("Exit prompter");
}

describe("prompter layout", () => {
  it("renders the strip instead of the full panel", async () => {
    await renderPrompter();
    expect(screen.queryByLabelText("Question heard")).toBeNull();
    expect(screen.queryByLabelText("Type a question")).toBeNull();
    expect(screen.getByRole("button", { name: "Record" })).toBeInTheDocument();
    expect(screen.getByLabelText("Suggested answer")).toBeInTheDocument();
  });

  it("streams the answer into the strip at the persisted text size", async () => {
    await renderPrompter();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: "s1", text: "Tell me about React", isFinal: true });
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "Use hooks." });
    await settle();
    await screen.findByText("Use hooks.");
    const body = screen.getByLabelText("Suggested answer");
    expect(body).toHaveStyle({ fontSize: "18px" });
    // The one-line question shows the transcript with the full text as a title.
    expect(screen.getByTitle("Tell me about React")).toBeInTheDocument();
  });

  it("keeps the answer top-anchored while streaming and resets per entry", async () => {
    await renderPrompter();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    const body = screen.getByLabelText("Suggested answer");
    Object.defineProperty(body, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(body, "clientHeight", { value: 200, configurable: true });
    body.scrollTop = 0;
    emit("llm:delta", { sessionId: "s1", delta: "line one\n\nline two" });
    await settle();
    await screen.findByText("line one");
    expect(body.scrollTop).toBe(0); // never yanked to the bottom mid-read
  });

  it("text size buttons persist prompterFontPx in steps and clamp at the range", async () => {
    await renderPrompter();
    fireEvent.click(screen.getByLabelText("Larger text"));
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ prompterFontPx: 20 });
    fireEvent.click(screen.getByLabelText("Smaller text"));
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ prompterFontPx: 16 });
  });

  it("smaller text is disabled at the minimum size", async () => {
    api.get_settings.mockResolvedValue(
      ok(settingsView({ layoutMode: "prompter", prompterFontPx: 14 })),
    );
    render(<App />);
    await waitForSettings();
    await screen.findByLabelText("Exit prompter");
    expect(screen.getByLabelText("Smaller text")).toBeDisabled();
    expect(screen.getByLabelText("Larger text")).toBeEnabled();
  });

  it("Dock under camera calls the window-docking command", async () => {
    await renderPrompter();
    fireEvent.click(screen.getByLabelText("Dock under camera"));
    await settle();
    expect(api.dock_window).toHaveBeenCalledTimes(1);
  });

  it("Exit prompter (and Escape) switch the layout back to full", async () => {
    await renderPrompter();
    api.set_settings.mockResolvedValue(ok(settingsView({ layoutMode: "full" })));
    fireEvent.keyDown(window, { key: "Escape" });
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ layoutMode: "full" });
    await screen.findByLabelText("Question heard");
    // And the header button takes us back in.
    api.set_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
    fireEvent.click(screen.getByLabelText("Enter prompter mode"));
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ layoutMode: "prompter" });
    await screen.findByLabelText("Exit prompter");
  });

  it("the global hotkey still toggles recording in the strip", async () => {
    await renderPrompter();
    emit("hotkey:toggle", {});
    await waitFor(() => expect(api.start_session).toHaveBeenCalledTimes(1));
    await screen.findByRole("button", { name: "Stop & Answer" });
  });

  it("errors and the protection verdict surface as alerts in the strip", async () => {
    await renderPrompter();
    emit("protection:failed", {});
    await screen.findByText(/Not hidden from screen share/);
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("session:error", {
      sessionId: "s1",
      error: { code: "stt_connect", message: "Could not connect to Deepgram." },
    });
    await screen.findByText("Could not connect to Deepgram.");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() =>
      expect(screen.queryByText("Could not connect to Deepgram.")).toBeNull(),
    );
  });
});

describe("full layout header", () => {
  it("shows the active provider and offers dock + prompter", async () => {
    render(<App />);
    await waitForSettings();
    expect(screen.getByText("Claude Haiku 4.5")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Dock under camera"));
    await settle();
    expect(api.dock_window).toHaveBeenCalledTimes(1);
  });

  it("puts the answer above the question and the controls", async () => {
    render(<App />);
    await waitForSettings();
    const answer = screen.getByLabelText("Suggested answer");
    const question = screen.getByLabelText("Question heard");
    const record = screen.getByRole("button", { name: "Record" });
    expect(answer.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(question.compareDocumentPosition(record) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("answer text size buttons persist answerFontPx", async () => {
    render(<App />);
    await waitForSettings();
    fireEvent.click(screen.getByLabelText("Larger text"));
    await settle();
    expect(api.set_settings).toHaveBeenLastCalledWith({ answerFontPx: 16 });
  });

  it("errors can be dismissed", async () => {
    api.ask.mockResolvedValueOnce({
      ok: false,
      error: { code: "no_llm_key", message: "No API key for Claude." },
    });
    render(<App />);
    await waitForSettings();
    fireEvent.change(screen.getByLabelText("Type a question"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("No API key for Claude.");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});
