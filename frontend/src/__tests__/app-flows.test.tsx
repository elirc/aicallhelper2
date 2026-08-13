import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { emit, err, installMockApi, ok, settle, type MockApi } from "./testutils";

let api: MockApi;

beforeEach(() => {
  api = installMockApi();
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function renderApp() {
  const view = render(<App />);
  await screen.findByText(/Ready — press Record/);
  return view;
}

const METRICS = { sttFinalizeMs: 210, firstTokenMs: 940, totalMs: 2130 };

describe("record flow", () => {
  it("walks idle -> starting -> recording -> finalizing -> answer -> done", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(api.start_session).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Recording call audio…")).toBeInTheDocument();

    emit("stt:partial", { sessionId: "s1", text: "tell me about", isFinal: false });
    expect(screen.getByText("tell me about")).toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(api.stop_session).toHaveBeenCalledWith("s1");
    expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();

    emit("llm:delta", { sessionId: "s1", delta: "Hello **wor" });
    emit("llm:delta", { sessionId: "s1", delta: "ld**." });
    await waitFor(() =>
      expect(screen.getByText("Generating answer…")).toBeInTheDocument(),
    );

    emit("llm:done", {
      sessionId: "s1",
      transcript: "Tell me about yourself.",
      answer: "Hello **world**.",
      metrics: METRICS,
    });
    await settle();
    expect(screen.getByText(/Done — press Record/)).toBeInTheDocument();
    expect(screen.getByText("world")).toBeInTheDocument(); // markdown bold rendered
    expect(screen.getByText("0.9s to first word")).toBeInTheDocument();
    expect(screen.getByText("Tell me about yourself.")).toBeInTheDocument();
  });

  it("level meter and timer appear only while recording", async () => {
    await renderApp();
    expect(screen.queryByTestId("level-meter")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("audio:level", { sessionId: "s1", rms: 0.5 });
    expect(screen.getByTestId("level-meter")).toBeInTheDocument();
    expect(screen.getByText("0:00")).toBeInTheDocument();
  });

  it("events arriving before the start promise resolved are replayed on adopt", async () => {
    let resolveStart: (value: unknown) => void = () => {};
    api.start_session.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStart = resolve;
      }),
    );
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    // The core already streams for s1, but the frontend has no id yet.
    emit("stt:partial", { sessionId: "s1", text: "early words", isFinal: false });
    resolveStart(ok("s1"));
    await settle();
    expect(screen.getByText("early words")).toBeInTheDocument();
  });

  it("stale events (wrong session id) change nothing, ever", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: "GHOST", text: "phantom text", isFinal: false });
    emit("session:error", {
      sessionId: "GHOST",
      error: { code: "stt_error", message: "phantom error" },
    });
    await settle();
    expect(screen.queryByText("phantom text")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a not-taken stop waits, then recovers instead of hanging in Finalizing", async () => {
    // It must NOT cancel: the core may have auto-stopped at the 120s cap and
    // still be producing the answer. Recovery is a bounded fallback.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      api.stop_session.mockResolvedValueOnce(err("internal", "Stop not taken"));
      await renderApp();
      fireEvent.click(screen.getByRole("button", { name: "Record" }));
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
      await settle();
      expect(api.cancel_session).not.toHaveBeenCalled();
      expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(21_000);
      });
      expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("start failure shows the actionable error", async () => {
    api.start_session.mockResolvedValueOnce(
      err("no_stt_key", "No Deepgram API key saved. Open Settings (gear icon) and add it."),
    );
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("No Deepgram API key saved");
    expect(screen.getByRole("button", { name: "Record" })).toBeInTheDocument();
  });

  it("the 120s cap event flips status to the cap message", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("session:autostopped", { sessionId: "s1" });
    expect(
      screen.getByText("Reached the 120s limit — answering now"),
    ).toBeInTheDocument();
  });
});

describe("errors", () => {
  it("session:error shows role=alert, returns to idle, keeps the partial answer", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "Partial answer text" });
    await waitFor(() =>
      expect(screen.getByText("Partial answer text")).toBeInTheDocument(),
    );
    emit("session:error", {
      sessionId: "s1",
      error: { code: "llm_http", message: "Could not reach Anthropic." },
    });
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not reach Anthropic.");
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    expect(screen.getByText("Partial answer text")).toBeInTheDocument(); // kept
  });

  it("an aborted error code is never shown", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("session:error", {
      sessionId: "s1",
      error: { code: "aborted", message: "should never render" },
    });
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("ask flow", () => {
  it("submits the trimmed question, clears the input on success", async () => {
    await renderApp();
    const input = screen.getByLabelText("Type a question");
    fireEvent.change(input, { target: { value: "  What is DI?  " } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    expect(api.ask).toHaveBeenCalledWith("What is DI?");
    expect(input).toHaveValue("");
    emit("stt:partial", { sessionId: "s1", text: "What is DI?", isFinal: true });
    emit("llm:done", {
      sessionId: "s1",
      transcript: "What is DI?",
      answer: "Dependency injection.",
      metrics: { ...METRICS, sttFinalizeMs: 0 },
    });
    await settle();
    expect(screen.getByText("Dependency injection.")).toBeInTheDocument();
  });

  it("keeps the input on failure so the user can retry", async () => {
    api.ask.mockResolvedValueOnce(err("no_llm_key", "No API key."));
    await renderApp();
    const input = screen.getByLabelText("Type a question");
    fireEvent.change(input, { target: { value: "my question" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    expect(input).toHaveValue("my question");
    expect(screen.getByRole("alert")).toHaveTextContent("No API key.");
  });

  it("empty and whitespace submits never reach the core", async () => {
    await renderApp();
    const input = screen.getByLabelText("Type a question");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    expect(api.ask).not.toHaveBeenCalled();
  });

  it("is disabled while recording but enabled while answering", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(screen.getByLabelText("Type a question")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(screen.getByLabelText("Type a question")).toBeDisabled(); // finalizing
    emit("llm:delta", { sessionId: "s1", delta: "streaming…" });
    await waitFor(() =>
      expect(screen.getByLabelText("Type a question")).toBeEnabled(),
    ); // answering: asking over a streaming answer supersedes it
  });
});
