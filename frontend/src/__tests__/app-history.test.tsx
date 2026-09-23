import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { emit, installMockApi, ok, settle, type MockApi } from "./testutils";

let api: MockApi;
let sidCounter: number;

beforeEach(() => {
  api = installMockApi();
  sidCounter = 0;
});

async function renderApp() {
  render(<App />);
  await screen.findByText(/Ready — press Record/);
}

async function askRound(question: string, answer: string): Promise<string> {
  sidCounter += 1;
  const sid = `s${sidCounter}`;
  api.ask.mockResolvedValueOnce(ok(sid));
  const input = screen.getByLabelText("Type a question");
  fireEvent.change(input, { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await settle();
  emit("stt:partial", { sessionId: sid, text: question, isFinal: true });
  emit("llm:done", {
    sessionId: sid,
    transcript: question,
    answer,
    metrics: { sttFinalizeMs: 0, firstTokenMs: 500, totalMs: 900 },
  });
  await settle();
  return sid;
}

describe("history", () => {
  it("offers Clear from one entry; navigation only from two", async () => {
    await renderApp();
    expect(screen.queryByText("Clear")).toBeNull();
    await askRound("q one", "a one");
    // A single answer must be clearable too (review §5).
    expect(screen.getByText("Clear")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Previous answer" })).toBeNull();
    expect(screen.queryByText("1/1")).toBeNull();
    await askRound("q two", "a two");
    expect(screen.getByText("Clear")).toBeInTheDocument();
    expect(screen.getByText("2/2")).toBeInTheDocument();
  });

  it("navigates with prev/next and shows the viewed entry", async () => {
    await renderApp();
    await askRound("q one", "answer one");
    await askRound("q two", "answer two");
    expect(screen.getByText("answer two")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous answer" }));
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.getByText("answer one")).toBeInTheDocument();
    expect(screen.queryByText("answer two")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Next answer" }));
    expect(screen.getByText("answer two")).toBeInTheDocument();
  });

  it("keeps only the last 6 entries", async () => {
    await renderApp();
    for (let i = 1; i <= 7; i += 1) {
      await askRound(`question ${i}`, `answer ${i}`);
    }
    expect(screen.getByText("6/6")).toBeInTheDocument();
    // Walk to the oldest survivor: entry 2, not entry 1.
    for (let i = 0; i < 5; i += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Previous answer" }));
    }
    expect(screen.getByText("answer 2")).toBeInTheDocument();
    expect(screen.queryByText("answer 1")).toBeNull();
  });

  it("a new recording jumps the view to the live entry", async () => {
    await renderApp();
    await askRound("q one", "answer one");
    await askRound("q two", "answer two");
    fireEvent.click(screen.getByRole("button", { name: "Previous answer" }));
    expect(screen.getByText("answer one")).toBeInTheDocument();
    await askRound("q three", "answer three");
    expect(screen.getByText("answer three")).toBeInTheDocument();
    expect(screen.getByText("3/3")).toBeInTheDocument();
  });

  it("clear wipes everything, announces, and moves focus to Record", async () => {
    await renderApp();
    await askRound("q one", "a one");
    await askRound("q two", "a two");
    fireEvent.click(screen.getByText("Clear"));
    expect(screen.queryByText("2/2")).toBeNull();
    expect(screen.queryByText("a two")).toBeNull();
    expect(screen.getByText("History cleared")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record" })).toHaveFocus();
  });

  it("an aborted attempt that captured nothing is discarded", async () => {
    await renderApp();
    await askRound("q one", "a one");
    // Start a recording that errors before any speech.
    api.start_session.mockResolvedValueOnce(ok("s99"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("session:error", {
      sessionId: "s99",
      error: { code: "stt_connect", message: "Could not connect." },
    });
    await settle();
    // Only the one real entry remains -> no navigation, no husk entry.
    expect(screen.queryByRole("button", { name: "Previous answer" })).toBeNull();
    expect(screen.getByText("a one")).toBeInTheDocument();
  });

  it("a failed attempt that captured a question is retired into history", async () => {
    await renderApp();
    await askRound("q one", "a one");
    api.start_session.mockResolvedValueOnce(ok("s99"));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    emit("stt:partial", { sessionId: "s99", text: "half a question", isFinal: false });
    emit("session:error", {
      sessionId: "s99",
      error: { code: "stt_error", message: "died" },
    });
    await settle();
    expect(screen.getByText("2/2")).toBeInTheDocument(); // transcript is user work
    expect(screen.getByText("half a question")).toBeInTheDocument();
  });
});

describe("regenerate", () => {
  it("re-asks the viewed question as a new history entry", async () => {
    await renderApp();
    await askRound("original question", "first answer");
    api.ask.mockResolvedValueOnce(ok("s50"));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await settle();
    expect(api.ask).toHaveBeenLastCalledWith("original question");
    emit("llm:done", {
      sessionId: "s50",
      transcript: "original question",
      answer: "second answer",
      metrics: { sttFinalizeMs: 0, firstTokenMs: 400, totalMs: 800 },
    });
    await settle();
    expect(screen.getByText("second answer")).toBeInTheDocument();
    expect(screen.getByText("2/2")).toBeInTheDocument(); // NEW entry
  });

  it("is hidden while recording and when no question exists", async () => {
    await renderApp();
    expect(screen.queryByRole("button", { name: "Regenerate" })).toBeNull();
    await askRound("q", "a");
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    await settle();
    expect(screen.queryByRole("button", { name: "Regenerate" })).toBeNull();
  });
});
