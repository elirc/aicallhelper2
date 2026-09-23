/**
 * R04 / R07 / R08 and §4: overlapping commands, stale responses, duplicate
 * deliveries, refused-stop recovery, and Stop while Settings is open. Each
 * test interleaves bridge promises by hand, which no earlier test did.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
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

const METRICS = { sttFinalizeMs: 1, firstTokenMs: 2, totalMs: 3 };

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
  await waitForSettings();
}

/** Start A (held), abort it, start B (held). Returns both resolvers. */
async function startAbortStart() {
  const a = deferred();
  const b = deferred();
  api.start_session.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
  fireEvent.click(screen.getByRole("button", { name: "Record" }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Starting…" })); // abort A
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Record" })); // start B
  await settle();
  return { a, b };
}

describe("stale start responses (R04)", () => {
  it("a stale start-aborted does not null the newer session's id", async () => {
    await renderApp();
    const { a, b } = await startAbortStart();
    b.resolve(ok("s2"));
    await settle();
    expect(screen.getByRole("button", { name: "Stop & Answer" })).toBeInTheDocument();
    a.resolve(err("aborted", "Superseded by a newer request."));
    await settle();
    // B is still the tracked session: its button and its events survive.
    expect(screen.getByRole("button", { name: "Stop & Answer" })).toBeInTheDocument();
    emit("stt:partial", { sessionId: "s2", text: "words for B", isFinal: false });
    expect(screen.getByText("words for B")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(api.stop_session).toHaveBeenCalledWith("s2");
  });

  it("a stale start-failed does not flip the newer session to idle or show its error", async () => {
    await renderApp();
    const { a, b } = await startAbortStart();
    b.resolve(ok("s2"));
    await settle();
    a.resolve(err("stt_connect", "Could not connect (stale)."));
    await settle();
    expect(screen.getByText("Recording call audio…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop & Answer" })).toBeInTheDocument();
    expect(screen.queryByText("Could not connect (stale).")).toBeNull();
  });

  it("an explicit abort is honoured even after a newer start began", async () => {
    // abortStartRef used to be reset by the next start, so A resolving ok
    // was ADOPTED and never cancelled.
    await renderApp();
    const { a, b } = await startAbortStart();
    a.resolve(ok("s1"));
    await settle();
    expect(api.cancel_session).toHaveBeenCalledWith("s1");
    expect(screen.getByRole("button", { name: "Starting…" })).toBeInTheDocument();
    b.resolve(ok("s2"));
    await settle();
    expect(api.cancel_session).not.toHaveBeenCalledWith("s2");
    emit("stt:partial", { sessionId: "s2", text: "B heard", isFinal: false });
    expect(screen.getByText("B heard")).toBeInTheDocument();
  });

  it("a stale response does not wipe events buffered for the newer command", async () => {
    await renderApp();
    const { a, b } = await startAbortStart();
    emit("stt:partial", { sessionId: "s2", text: "early for B", isFinal: false });
    a.resolve(err("aborted", "Superseded by a newer request."));
    await settle();
    b.resolve(ok("s2"));
    await settle();
    expect(screen.getByText("early for B")).toBeInTheDocument();
  });
});

describe("overlapping asks (R04)", () => {
  it("only the latest ask is adopted; the stale one's session is cancelled", async () => {
    await renderApp();
    const a = deferred();
    const b = deferred();
    api.ask.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const input = screen.getByLabelText("Type a question");
    fireEvent.change(input, { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    fireEvent.change(input, { target: { value: "second" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    b.resolve(ok("s2"));
    await settle();
    a.resolve(ok("s1")); // stale success arrives last
    await settle();
    expect(api.cancel_session).toHaveBeenCalledWith("s1");
    emit("llm:done", { sessionId: "s2", transcript: "second", answer: "answer two", metrics: METRICS });
    await settle();
    expect(screen.getByText("answer two")).toBeInTheDocument();
    emit("llm:done", { sessionId: "s1", transcript: "first", answer: "answer one", metrics: METRICS });
    await settle();
    expect(screen.queryByText("answer one")).toBeNull();
  });

  it("a stale ask failure shows no error over the newer answer", async () => {
    await renderApp();
    const a = deferred();
    api.ask.mockReturnValueOnce(a.promise).mockResolvedValueOnce(ok("s2"));
    const input = screen.getByLabelText("Type a question");
    fireEvent.change(input, { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    fireEvent.change(input, { target: { value: "second" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    a.resolve(err("llm_http", "Stale failure."));
    await settle();
    expect(screen.queryByText("Stale failure.")).toBeNull();
    expect(screen.getByText("Generating answer…")).toBeInTheDocument();
  });
});

describe("quick settings ordering (R04)", () => {
  it("two fast text-size clicks step twice, applied in click order", async () => {
    await renderApp();
    const first = deferred();
    api.set_settings.mockReturnValueOnce(first.promise);
    fireEvent.click(screen.getByLabelText("Larger text"));
    fireEvent.click(screen.getByLabelText("Larger text"));
    await settle();
    // Serialized: the second patch waits for the first.
    expect(api.set_settings).toHaveBeenCalledTimes(1);
    expect(api.set_settings).toHaveBeenLastCalledWith({ answerFontPx: 16 });
    api.set_settings.mockResolvedValueOnce(ok(settingsView({ answerFontPx: 18 })));
    first.resolve(ok(settingsView({ answerFontPx: 16 })));
    await settle();
    await settle();
    expect(api.set_settings).toHaveBeenCalledTimes(2);
    expect(api.set_settings).toHaveBeenLastCalledWith({ answerFontPx: 18 });
    await waitFor(() =>
      expect(screen.getByLabelText("Suggested answer").querySelector(".answer-body")).toHaveStyle({
        fontSize: "18px",
      }),
    );
  });
});

describe("duplicate and out-of-order deliveries (R07)", () => {
  it("a re-delivered delta (same seq) is not painted twice, even when the answer errors", async () => {
    await renderApp();
    api.ask.mockResolvedValueOnce(ok("s1"));
    fireEvent.change(screen.getByLabelText("Type a question"), { target: { value: "q" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "Hello", seq: 10 });
    emit("llm:delta", { sessionId: "s1", delta: "Hello", seq: 10 }); // batch retry
    emit("llm:delta", { sessionId: "s1", delta: " world", seq: 11 });
    emit("llm:delta", { sessionId: "s1", delta: " late", seq: 9 }); // stale straggler
    emit("session:error", {
      sessionId: "s1",
      seq: 12,
      error: { code: "llm_http", message: "Stream broke." },
    });
    await settle();
    // No llm:done repairs it here, so the kept partial must already be right.
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("events without seq are still accepted (older core)", async () => {
    await renderApp();
    api.ask.mockResolvedValueOnce(ok("s1"));
    fireEvent.change(screen.getByLabelText("Type a question"), { target: { value: "q" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    emit("llm:done", { sessionId: "s1", transcript: "q", answer: "plain", metrics: METRICS });
    await settle();
    expect(screen.getByText("plain")).toBeInTheDocument();
  });
});

describe("refused-stop recovery (R08)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  async function strandedRecording(sid = "s1") {
    api.start_session.mockResolvedValueOnce(ok(sid));
    api.stop_session.mockResolvedValueOnce(err("internal", "Stop not taken"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: sid, text: "the question", isFinal: true });
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
  }

  it("an answer streaming past 20 s survives: a delta disarms recovery", async () => {
    await renderApp();
    await strandedRecording();
    emit("llm:delta", { sessionId: "s1", delta: "part one" });
    await settle();
    act(() => {
      vi.advanceTimersByTime(25_000);
    });
    emit("llm:delta", { sessionId: "s1", delta: " part two" });
    emit("llm:done", {
      sessionId: "s1",
      transcript: "the question",
      answer: "part one part two",
      metrics: METRICS,
    });
    await settle();
    expect(screen.getByText("part one part two")).toBeInTheDocument();
    expect(api.cancel_session).not.toHaveBeenCalled();
  });

  it("an Ask accepted after a refused stop is not killed by the old timer", async () => {
    await renderApp();
    await strandedRecording();
    emit("llm:delta", { sessionId: "s1", delta: "streaming" }); // phase -> answering
    await settle();
    api.ask.mockResolvedValueOnce(ok("s2"));
    fireEvent.change(screen.getByLabelText("Type a question"), { target: { value: "next" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    act(() => {
      vi.advanceTimersByTime(25_000);
    });
    emit("llm:done", { sessionId: "s2", transcript: "next", answer: "answer C", metrics: METRICS });
    await settle();
    expect(screen.getByText("answer C")).toBeInTheDocument();
    expect(api.cancel_session).not.toHaveBeenCalledWith("s2");
  });

  it("recovery cancels the core session so UI and core agree", async () => {
    await renderApp();
    await strandedRecording();
    act(() => {
      vi.advanceTimersByTime(21_000);
    });
    await settle();
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    expect(api.cancel_session).toHaveBeenCalledWith("s1");
  });

  it("asks the core first and keeps waiting while it reports the session alive", async () => {
    const idle = ok(statusSnapshot({ session: { id: null, phase: "idle" } }));
    api.get_status = vi
      .fn()
      .mockResolvedValueOnce(idle) // page-load snapshot
      .mockResolvedValueOnce(ok(statusSnapshot({ session: { id: "s1", phase: "finalizing" } })))
      .mockResolvedValue(idle);
    await renderApp();
    await settle();
    await strandedRecording();
    await act(async () => {
      vi.advanceTimersByTime(21_000);
    });
    await settle();
    expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument(); // core: still alive
    await act(async () => {
      vi.advanceTimersByTime(21_000);
    });
    await settle();
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument(); // core: gone
  });

  it("an 'unknown' status (stalled core loop) waits again instead of cancelling", async () => {
    const idle = ok(statusSnapshot({ session: { id: null, phase: "idle" } }));
    api.get_status = vi
      .fn()
      .mockResolvedValueOnce(idle) // page-load snapshot
      .mockResolvedValueOnce(ok(statusSnapshot({ session: { id: null, phase: "unknown" } })))
      .mockResolvedValue(idle);
    await renderApp();
    await settle();
    await strandedRecording();
    await act(async () => {
      vi.advanceTimersByTime(21_000);
    });
    await settle();
    expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();
    expect(api.cancel_session).not.toHaveBeenCalled(); // "don't know" is not "dead"
    await act(async () => {
      vi.advanceTimersByTime(21_000);
    });
    await settle();
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    expect(api.cancel_session).toHaveBeenCalledWith("s1");
  });
});

describe("Stop stays reachable while Settings is open (§4)", () => {
  async function recordThenOpenSettings() {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByText("Deepgram API key");
  }

  it("the global hotkey stops a recording running behind Settings", async () => {
    await recordThenOpenSettings();
    emit("hotkey:toggle", {});
    await settle();
    expect(api.stop_session).toHaveBeenCalledWith("s1");
  });

  it("Settings shows the recording and a Stop & Answer button", async () => {
    await recordThenOpenSettings();
    expect(screen.getByText("Recording call audio…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(api.stop_session).toHaveBeenCalledWith("s1");
  });
});

describe("prompter finalization status (§5)", () => {
  it("shows Finalizing in the strip instead of a dead Record button", async () => {
    api.get_settings.mockResolvedValue(ok(settingsView({ layoutMode: "prompter" })));
    render(<App />);
    await screen.findByLabelText("Exit prompter");
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(screen.getByRole("button", { name: "Finalizing…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();
  });
});

describe("recording countdown (R10 contract: session:recording)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  async function record() {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
  }

  it("follows the core's deadline, not the local tick", async () => {
    await record();
    expect(screen.queryByText(/ left$/)).toBeNull();
    // The core armed its cap when capture started; only 25 s remain even
    // though the local tick has barely begun.
    emit("session:recording", { sessionId: "s1", deadlineMs: Date.now() + 25_000, capMs: 120_000 });
    expect(screen.getByText("0:25 left")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(screen.getByText("0:15 left")).toBeInTheDocument();
  });

  it("ignores a deadline for another session", async () => {
    await record();
    emit("session:recording", { sessionId: "s0", deadlineMs: Date.now() + 5_000, capMs: 120_000 });
    expect(screen.queryByText(/ left$/)).toBeNull();
  });

  it("falls back to the local tick when the core sends no deadline", async () => {
    await record();
    act(() => {
      vi.advanceTimersByTime(95_000);
    });
    expect(screen.getByText(/^0:2\d left$/)).toBeInTheDocument();
  });
});

describe("answer finish notice (R03 contract)", () => {
  async function answerWith(finish: string | undefined) {
    await renderApp();
    api.ask.mockResolvedValueOnce(ok("s1"));
    fireEvent.change(screen.getByLabelText("Type a question"), { target: { value: "q" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    emit("llm:done", {
      sessionId: "s1",
      transcript: "q",
      answer: "a partial answer",
      metrics: METRICS,
      ...(finish ? { finish } : {}),
    });
    await settle();
  }

  it("marks a truncated answer, keeping its text and Copy", async () => {
    await answerWith("truncated");
    expect(screen.getByText(/Answer cut off at the length limit/)).toBeInTheDocument();
    expect(screen.getByText("a partial answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("marks a refused answer", async () => {
    await answerWith("refused");
    expect(screen.getByText("The model declined to finish this answer.")).toBeInTheDocument();
  });

  it("says nothing for a complete or unstamped answer", async () => {
    await answerWith(undefined);
    expect(screen.queryByText(/cut off|declined/)).toBeNull();
  });
});
