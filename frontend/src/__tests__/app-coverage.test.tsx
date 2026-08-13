/**
 * Gaps the 15-agent audit named precisely: behaviors that were implemented
 * but that no test would have caught a regression in.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { copyToClipboard } from "../clipboard";
import { emit, err, installMockApi, ok, settle, type MockApi } from "./testutils";

let api: MockApi;

beforeEach(() => {
  api = installMockApi();
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
  Reflect.deleteProperty(document, "execCommand");
});

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
}

const METRICS = { sttFinalizeMs: 0, firstTokenMs: 400, totalMs: 800 };

async function askRound(sid: string, question: string, answer: string) {
  api.ask.mockResolvedValueOnce(ok(sid));
  fireEvent.change(screen.getByLabelText("Type a question"), {
    target: { value: question },
  });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await settle();
  emit("llm:done", { sessionId: sid, transcript: question, answer, metrics: METRICS });
  await settle();
}

describe("clipboard fallback failure", () => {
  it("throws when execCommand reports failure, so the UI can report it", async () => {
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => false),
      configurable: true,
    });
    await expect(copyToClipboard("text")).rejects.toThrow();
  });

  it("surfaces that failure in the error box rather than a false 'Copied'", async () => {
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => false),
      configurable: true,
    });
    await renderApp();
    await askRound("s1", "q", "an answer");
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not copy to the clipboard.");
    expect(screen.queryByText("Copied ✓")).toBeNull();
  });

  it("leaves no stray textarea behind on either path", async () => {
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => true),
      configurable: true,
    });
    await copyToClipboard("text");
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => false),
      configurable: true,
    });
    await expect(copyToClipboard("text")).rejects.toThrow();
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });
});

describe("pre-adoption event buffering on the ask path", () => {
  it("replays events that beat the ask promise", async () => {
    // The core can emit for the new session before the JS promise resolves —
    // two independent channels. Only start_session was covered.
    let resolveAsk: (value: unknown) => void = () => {};
    api.ask.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveAsk = resolve;
      }),
    );
    await renderApp();
    fireEvent.change(screen.getByLabelText("Type a question"), {
      target: { value: "the question" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "early answer text" });
    resolveAsk(ok("s1"));
    await settle();
    await waitFor(() =>
      expect(screen.getByText("early answer text")).toBeInTheDocument(),
    );
  });

  it("discards buffered events when the ask is rejected", async () => {
    let rejectAsk: (value: unknown) => void = () => {};
    api.ask.mockReturnValueOnce(
      new Promise((resolve) => {
        rejectAsk = resolve;
      }),
    );
    await renderApp();
    fireEvent.change(screen.getByLabelText("Type a question"), {
      target: { value: "the question" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "orphan text" });
    rejectAsk(err("no_llm_key", "No API key."));
    await settle();
    expect(screen.queryByText("orphan text")).toBeNull();
    // A later, unrelated session must not inherit the orphaned buffer.
    await askRound("s2", "second", "second answer");
    expect(screen.getByText("second answer")).toBeInTheDocument();
    expect(screen.queryByText("orphan text")).toBeNull();
  });

  it("discards buffered events when a start fails", async () => {
    let resolveStart: (value: unknown) => void = () => {};
    api.start_session.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStart = resolve;
      }),
    );
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: "s1", text: "orphan transcript", isFinal: false });
    resolveStart(err("no_stt_key", "No Deepgram API key saved."));
    await settle();
    expect(screen.queryByText("orphan transcript")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("No Deepgram API key saved.");
  });
});

describe("stop-not-taken keeps captured work", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("retires a transcript the user already spoke", async () => {
    api.stop_session.mockResolvedValueOnce(err("internal", "Stop not taken"));
    await renderApp();
    await askRound("s0", "earlier question", "earlier answer");
    api.start_session.mockResolvedValueOnce(ok("s1"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: "s1", text: "a real question", isFinal: true });
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    act(() => {
      vi.advanceTimersByTime(21_000); // the bounded fallback, no answer came
    });
    // Recovered to idle AND the transcript survived as history (user work).
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    expect(screen.getByText("2/2")).toBeInTheDocument();
    expect(screen.getByText("a real question")).toBeInTheDocument();
  });

  it("discards an attempt that captured nothing", async () => {
    api.stop_session.mockResolvedValueOnce(err("internal", "Stop not taken"));
    await renderApp();
    await askRound("s0", "earlier question", "earlier answer");
    api.start_session.mockResolvedValueOnce(ok("s1"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    act(() => {
      vi.advanceTimersByTime(21_000);
    });
    expect(screen.queryByText("2/2")).toBeNull(); // no empty husk entry
    expect(screen.getByText("earlier answer")).toBeInTheDocument();
  });
});

describe("regenerate targets the VIEWED entry", () => {
  it("re-asks the entry you navigated back to, not the newest one", async () => {
    await renderApp();
    await askRound("s1", "first question", "first answer");
    await askRound("s2", "second question", "second answer");
    fireEvent.click(screen.getByRole("button", { name: "Previous answer" }));
    expect(screen.getByText("first answer")).toBeInTheDocument();

    api.ask.mockResolvedValueOnce(ok("s3"));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await settle();
    expect(api.ask).toHaveBeenLastCalledWith("first question");
    emit("llm:done", {
      sessionId: "s3",
      transcript: "first question",
      answer: "regenerated answer",
      metrics: METRICS,
    });
    await settle();
    expect(screen.getByText("regenerated answer")).toBeInTheDocument();
    expect(screen.getByText("3/3")).toBeInTheDocument(); // a NEW entry
    // The original answer is kept for comparison, not overwritten — only the
    // VIEWED entry renders, so navigate back to prove it survived.
    fireEvent.click(screen.getByRole("button", { name: "Previous answer" }));
    fireEvent.click(screen.getByRole("button", { name: "Previous answer" }));
    expect(screen.getByText("first answer")).toBeInTheDocument();
  });

  it("stays available while an answer is streaming, superseding it", async () => {
    await renderApp();
    await askRound("s1", "a question", "an answer");
    api.ask.mockResolvedValueOnce(ok("s2"));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await settle();
    emit("llm:delta", { sessionId: "s2", delta: "streaming…" });
    await waitFor(() => expect(screen.getByText("streaming…")).toBeInTheDocument());
    // Visible during 'answering' per the contract.
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeInTheDocument();
    api.ask.mockResolvedValueOnce(ok("s3"));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await settle();
    expect(api.ask).toHaveBeenCalledTimes(3);
  });

  it("is hidden for an entry with no question", async () => {
    await renderApp();
    expect(screen.queryByRole("button", { name: "Regenerate" })).toBeNull();
  });
});

describe("a stop that races the 120s cap", () => {
  it("does not destroy the answer the cap is already producing", async () => {
    // The cap auto-stops in the core, so a Stop press already in flight is
    // refused. Treating that as "session is gone" and cancelling threw away
    // the answer to a question the user spent two minutes asking.
    api.stop_session.mockResolvedValueOnce(err("internal", "Stop not taken"));
    await renderApp();
    api.start_session.mockResolvedValueOnce(ok("s1"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: "s1", text: "a long question", isFinal: true });
    // The core hit the cap and is already finalizing; the UI has not seen the
    // autostopped event yet, so the user's Stop press is refused.
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();

    expect(api.cancel_session).not.toHaveBeenCalled();
    // The session is still tracked, so its answer still lands.
    emit("llm:delta", { sessionId: "s1", delta: "the hard-won " });
    emit("llm:done", {
      sessionId: "s1",
      transcript: "a long question",
      answer: "the hard-won answer",
      metrics: { sttFinalizeMs: 300, firstTokenMs: 900, totalMs: 1500 },
    });
    await settle();
    expect(screen.getByText("the hard-won answer")).toBeInTheDocument();
    expect(screen.getByText(/Done — press Record/)).toBeInTheDocument();
  });

  it("still recovers when the session really is gone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      api.stop_session.mockResolvedValueOnce(err("internal", "Stop not taken"));
      render(<App />);
      await screen.findByText(/Ready — press Record/);
      api.start_session.mockResolvedValueOnce(ok("s1"));
      fireEvent.click(screen.getByRole("button", { name: "Record" }));
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
      await settle();
      expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();
      // Nothing ever arrives: the bounded fallback returns us to idle rather
      // than hanging in "Finalizing…" forever.
      act(() => {
        vi.advanceTimersByTime(21_000);
      });
      expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
