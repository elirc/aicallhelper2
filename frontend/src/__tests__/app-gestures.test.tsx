import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { emit, err, installMockApi, ok, settle, type MockApi } from "./testutils";

let api: MockApi;

beforeEach(() => {
  api = installMockApi();
});

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
}

describe("Record during each phase", () => {
  it("during 'starting' aborts silently and cancels the late-resolving session", async () => {
    let resolveStart: (value: unknown) => void = () => {};
    api.start_session.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveStart = resolve;
      }),
    );
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    // System OUTPUT is captured, never a microphone — the copy must say so.
    expect(screen.getByText("Starting system-audio capture…")).toBeInTheDocument();
    expect(screen.queryByText(/microphone/i)).toBeNull();
    expect(screen.getByLabelText("Type a question")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Starting…" }));
    await settle();
    // Silent: back to idle, no error box.
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();

    // The session the core did create must be cancelled when it resolves.
    resolveStart(ok("s1"));
    await settle();
    expect(api.cancel_session).toHaveBeenCalledWith("s1");
    // And its events must not resurrect the UI.
    emit("stt:partial", { sessionId: "s1", text: "ghost", isFinal: false });
    await settle();
    expect(screen.queryByText("ghost")).toBeNull();
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
  });

  it("during 'finalizing' is ignored", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();
    // The button says what is happening instead of a Record it will ignore.
    const button = screen.getByRole("button", { name: "Finalizing…" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);
    await settle();
    expect(api.start_session).toHaveBeenCalledTimes(1); // ignored
    expect(screen.getByText("Finalizing transcript…")).toBeInTheDocument();
  });

  it("during 'answering' starts a NEW session that supersedes the stream", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "streaming answer" });
    await waitFor(() => expect(screen.getByText("streaming answer")).toBeInTheDocument());

    api.start_session.mockResolvedValueOnce(ok("s2"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(api.start_session).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Recording call audio…")).toBeInTheDocument();
    // The superseded session's late done must not paint.
    emit("llm:done", {
      sessionId: "s1",
      transcript: "old",
      answer: "old answer",
      metrics: { sttFinalizeMs: 1, firstTokenMs: 2, totalMs: 3 },
    });
    await settle();
    expect(screen.queryByText("old answer")).toBeNull();
  });

  it("a superseded command reporting 'aborted' is never shown as an error", async () => {
    // The core returns aborted when a newer command claimed while this one
    // was awaiting its key read.
    api.start_session.mockResolvedValueOnce(err("aborted", "Superseded by a newer request."));
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText(/Ready — press Record/)).toBeInTheDocument();
  });

  it("an aborted ask keeps the typed text and shows nothing", async () => {
    api.ask.mockResolvedValueOnce(err("aborted", "Superseded by a newer request."));
    await renderApp();
    const input = screen.getByLabelText("Type a question");
    fireEvent.change(input, { target: { value: "my question" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(input).toHaveValue("my question");
  });
});

describe("delta coalescing and ordering", () => {
  it("many deltas in one frame paint as one update, before the terminal event", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    // Fire deltas AND the done event in the same tick: the buffered deltas
    // must flush before done, or the answer would jump/flicker.
    for (const part of ["Hello", " ", "there", " ", "friend"]) {
      emit("llm:delta", { sessionId: "s1", delta: part });
    }
    emit("llm:done", {
      sessionId: "s1",
      transcript: "q",
      answer: "Hello there friend",
      metrics: { sttFinalizeMs: 10, firstTokenMs: 20, totalMs: 30 },
    });
    await settle();
    expect(screen.getByText("Hello there friend")).toBeInTheDocument();
    expect(screen.getByText(/Done — press Record/)).toBeInTheDocument();
  });

  it("deltas flush before an error so the partial answer survives", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "half an answer" });
    emit("session:error", {
      sessionId: "s1",
      error: { code: "llm_timeout", message: "Took too long." },
    });
    await settle();
    expect(screen.getByText("half an answer")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Took too long.");
  });
});

describe("answer panel scrolling", () => {
  function scrollState(el: HTMLElement, scrollTop: number, clientHeight: number, scrollHeight: number) {
    Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
    Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
    el.scrollTop = scrollTop;
  }

  async function streamingApp() {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "line one" });
    await waitFor(() => expect(screen.getByText("line one")).toBeInTheDocument());
    const panel = screen.getByLabelText("Suggested answer");
    return panel.querySelector(".answer-body") as HTMLElement;
  }

  it("sticks to the bottom while the user is at the bottom", async () => {
    const body = await streamingApp();
    scrollState(body, 100, 100, 200); // exactly at the bottom
    fireEvent.scroll(body);
    scrollState(body, 100, 100, 400); // content grew
    emit("llm:delta", { sessionId: "s1", delta: " more text" });
    await waitFor(() => expect(body.scrollTop).toBe(400));
  });

  it("does NOT yank the user down when they scrolled up to re-read", async () => {
    const body = await streamingApp();
    scrollState(body, 0, 100, 500); // scrolled to the top, far from bottom
    fireEvent.scroll(body);
    scrollState(body, 0, 100, 900);
    emit("llm:delta", { sessionId: "s1", delta: " even more" });
    await waitFor(() => expect(screen.getByText(/even more/)).toBeInTheDocument());
    expect(body.scrollTop).toBe(0); // never yanked down
  });

  it("treats within-28px-of-bottom as at the bottom", async () => {
    const body = await streamingApp();
    scrollState(body, 80, 100, 200); // 20px from the bottom
    fireEvent.scroll(body);
    scrollState(body, 80, 100, 300);
    emit("llm:delta", { sessionId: "s1", delta: " grow" });
    await waitFor(() => expect(body.scrollTop).toBe(300));
  });
});

describe("accessibility wiring", () => {
  it("answer panel is a polite live region, busy only while answering", async () => {
    await renderApp();
    const panel = screen.getByLabelText("Suggested answer");
    const body = panel.querySelector(".answer-body") as HTMLElement;
    expect(body).toHaveAttribute("aria-live", "polite");
    expect(body).toHaveAttribute("aria-busy", "false");
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Stop & Answer" }));
    await settle();
    emit("llm:delta", { sessionId: "s1", delta: "streaming" });
    await waitFor(() => expect(body).toHaveAttribute("aria-busy", "true"));
    emit("llm:done", {
      sessionId: "s1",
      transcript: "q",
      answer: "streaming",
      metrics: { sttFinalizeMs: 1, firstTokenMs: 2, totalMs: 3 },
    });
    await settle();
    expect(body).toHaveAttribute("aria-busy", "false");
  });

  it("the status line is a live status region", async () => {
    await renderApp();
    const statuses = screen.getAllByRole("status");
    expect(statuses.some((el) => el.textContent?.includes("Ready — press Record"))).toBe(true);
  });

  it("errors are alerts", async () => {
    api.start_session.mockResolvedValueOnce(err("no_stt_key", "No Deepgram API key saved."));
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

describe("copy confirmation timing", () => {
  it("'Copied ✓' reverts to 'Copy' after ~1.2s", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(async () => undefined) },
      configurable: true,
    });
    try {
      render(<App />);
      await screen.findByText(/Ready — press Record/);
      fireEvent.change(screen.getByLabelText("Type a question"), {
        target: { value: "q" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Ask" }));
      await settle();
      emit("llm:done", {
        sessionId: "s1",
        transcript: "q",
        answer: "an answer",
        metrics: { sttFinalizeMs: 0, firstTokenMs: 1, totalMs: 2 },
      });
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Copy" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Copied ✓" })).toBeInTheDocument(),
      );
      await waitFor(
        () => expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument(),
        { timeout: 4000 },
      );
    } finally {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });
});

describe("history guards", () => {
  it("Clear is disabled while a session is live", async () => {
    await renderApp();
    for (const [sid, text] of [
      ["s1", "one"],
      ["s2", "two"],
    ] as const) {
      api.ask.mockResolvedValueOnce(ok(sid));
      fireEvent.change(screen.getByLabelText("Type a question"), { target: { value: text } });
      fireEvent.click(screen.getByRole("button", { name: "Ask" }));
      await settle();
      emit("llm:done", {
        sessionId: sid,
        transcript: text,
        answer: `answer ${text}`,
        metrics: { sttFinalizeMs: 0, firstTokenMs: 1, totalMs: 2 },
      });
      await settle();
    }
    expect(screen.getByRole("button", { name: "Clear" })).toBeEnabled();
    api.start_session.mockResolvedValueOnce(ok("s3"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(screen.getByRole("button", { name: "Clear" })).toBeDisabled();
  });
});
