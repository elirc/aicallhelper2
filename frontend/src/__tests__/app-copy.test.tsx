import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { emit, installMockApi, settle } from "./testutils";

beforeEach(() => {
  installMockApi();
});

afterEach(() => {
  // jsdom has no clipboard by default; remove whatever a test installed.
  Reflect.deleteProperty(navigator, "clipboard");
  Reflect.deleteProperty(document, "execCommand");
});

const MARKDOWN_ANSWER = "Direct answer.\n\n- point **one**\n- point two";

async function answeredApp(): Promise<void> {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  const input = screen.getByLabelText("Type a question");
  fireEvent.change(input, { target: { value: "the question" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await settle();
  emit("llm:done", {
    sessionId: "s1",
    transcript: "the question",
    answer: MARKDOWN_ANSWER,
    metrics: { sttFinalizeMs: 0, firstTokenMs: 500, totalMs: 900 },
  });
  await settle();
}

describe("copy", () => {
  it("copies the markdown SOURCE (bullets survive pasting) and confirms", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    await answeredApp();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await settle();
    expect(writeText).toHaveBeenCalledWith(MARKDOWN_ANSWER); // source, not rendered text
    expect(screen.getByText("Copied ✓")).toBeInTheDocument();
    expect(screen.getByText("Answer copied to clipboard")).toBeInTheDocument();
  });

  it("falls back to execCommand when navigator.clipboard is missing (file://)", async () => {
    // The packaged app loads from file://, a non-secure context where
    // navigator.clipboard is undefined.
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", {
      value: execCommand,
      configurable: true,
    });
    await answeredApp();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await settle();
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(screen.getByText("Copied ✓")).toBeInTheDocument();
  });

  it("surfaces clipboard failure in the error box", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(async () => Promise.reject(new Error("denied"))) },
      configurable: true,
    });
    await answeredApp();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not copy to the clipboard.",
    );
    expect(screen.queryByText("Copied ✓")).toBeNull();
  });

  it("is hidden until an answer exists", async () => {
    render(<App />);
    await screen.findByText(/Ready — press Record/);
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});
