import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";

import { bridge, startHeartbeat, subscribeAppEvents } from "./bridge";
import { AnswerPanel } from "./components/AnswerPanel";
import { HistoryBar } from "./components/HistoryBar";
import { SettingsPanel } from "./components/SettingsPanel";
import { formatHotkey, formatMmSs } from "./format";
import type {
  AppErrorPayload,
  AppEventDetail,
  AnswerStyle,
  Metrics,
  SettingsView,
} from "./types";
import { hasKey } from "./types";

export type Phase = "idle" | "starting" | "recording" | "finalizing" | "answering";

export interface Entry {
  id: string;
  question: string;
  questionFinal: boolean;
  answer: string;
  metrics: Metrics | null;
  live: boolean;
}

interface State {
  phase: Phase;
  sessionId: string | null;
  entries: Entry[];
  view: number;
  error: AppErrorPayload | null;
  rms: number;
  seconds: number;
  capped: boolean;
  done: boolean;
  settingsOpen: boolean;
  settings: SettingsView | null;
  announcement: string;
}

const MAX_HISTORY = 6;

const initialState: State = {
  phase: "idle",
  sessionId: null,
  entries: [],
  view: 0,
  error: null,
  rms: 0,
  seconds: 0,
  capped: false,
  done: false,
  settingsOpen: false,
  settings: null,
  announcement: "",
};

type Action =
  | { type: "settings-loaded"; view: SettingsView }
  | { type: "start-pressed" }
  | { type: "start-accepted"; sid: string }
  | { type: "start-failed"; error: AppErrorPayload }
  | { type: "start-aborted" }
  | { type: "stop-accepted" }
  | { type: "stop-not-taken" }
  | { type: "ask-accepted"; sid: string; text: string }
  | { type: "error-set"; error: AppErrorPayload }
  | { type: "deltas"; sid: string; text: string }
  | { type: "event"; detail: AppEventDetail }
  | { type: "tick" }
  | { type: "settings-open" }
  | { type: "settings-close" }
  | { type: "history-nav"; delta: number }
  | { type: "history-clear" }
  | { type: "announce"; text: string };

/** Retire the live entry into history if it captured user work; discard an
 * attempt that captured nothing (whitespace-only counts as nothing). */
function retireLive(state: State): Pick<State, "entries" | "view"> {
  const liveIndex = state.entries.findIndex((entry) => entry.live);
  if (liveIndex === -1) return { entries: state.entries, view: state.view };
  const live = state.entries[liveIndex];
  if (!live) return { entries: state.entries, view: state.view };
  const captured = live.question.trim() !== "" || live.answer.trim() !== "";
  const entries = captured
    ? state.entries.map((entry, i) => (i === liveIndex ? { ...entry, live: false } : entry))
    : state.entries.filter((_, i) => i !== liveIndex);
  const view = Math.min(state.view, Math.max(0, entries.length - 1));
  return { entries, view };
}

function pushLive(state: State, entry: Entry): Pick<State, "entries" | "view"> {
  const retired = retireLive(state);
  let entries = [...retired.entries, entry];
  if (entries.length > MAX_HISTORY) {
    // Trim the oldest — never the in-flight entry (it is the one just pushed).
    entries = entries.slice(entries.length - MAX_HISTORY);
  }
  return { entries, view: entries.length - 1 };
}

function updateLive(state: State, sid: string, patch: Partial<Entry>): Entry[] {
  return state.entries.map((entry) =>
    entry.live && entry.id === sid ? { ...entry, ...patch } : entry,
  );
}

function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "settings-loaded":
      return { ...state, settings: action.view };
    case "start-pressed":
      return { ...state, phase: "starting", error: null, done: false, capped: false };
    case "start-accepted": {
      const placed = pushLive(state, {
        id: action.sid,
        question: "",
        questionFinal: false,
        answer: "",
        metrics: null,
        live: true,
      });
      return {
        ...state,
        ...placed,
        phase: "recording",
        sessionId: action.sid,
        seconds: 0,
        rms: 0,
      };
    }
    case "start-failed":
      return { ...state, phase: "idle", error: action.error };
    case "start-aborted":
      return { ...state, phase: "idle", sessionId: null };
    case "stop-accepted":
      return { ...state, phase: "finalizing" };
    case "stop-not-taken": {
      // The stop went nowhere (rule: this return is the only way we learn) —
      // recover instead of sitting in "Finalizing…" forever.
      const retired = retireLive(state);
      return { ...state, ...retired, phase: "idle", sessionId: null };
    }
    case "ask-accepted": {
      const placed = pushLive(state, {
        id: action.sid,
        question: action.text,
        questionFinal: true,
        answer: "",
        metrics: null,
        live: true,
      });
      return {
        ...state,
        ...placed,
        phase: "answering",
        sessionId: action.sid,
        error: null,
        done: false,
        capped: false,
      };
    }
    case "error-set":
      return { ...state, error: action.error };
    case "deltas": {
      if (state.sessionId !== action.sid) return state;
      const entries = state.entries.map((entry) =>
        entry.live && entry.id === action.sid
          ? { ...entry, answer: entry.answer + action.text }
          : entry,
      );
      return { ...state, entries, phase: "answering" };
    }
    case "event":
      return reduceEvent(state, action.detail);
    case "tick":
      return state.phase === "recording" ? { ...state, seconds: state.seconds + 1 } : state;
    case "settings-open":
      return { ...state, settingsOpen: true };
    case "settings-close":
      return { ...state, settingsOpen: false };
    case "history-nav": {
      const view = Math.min(
        Math.max(0, state.view + action.delta),
        Math.max(0, state.entries.length - 1),
      );
      return { ...state, view };
    }
    case "history-clear":
      if (state.phase !== "idle") return state;
      return {
        ...state,
        entries: [],
        view: 0,
        announcement: "History cleared",
        done: false,
        error: null,
      };
    case "announce":
      return { ...state, announcement: action.text };
  }
}

function reduceEvent(state: State, detail: AppEventDetail): State {
  const sid = detail.payload.sessionId;
  if (typeof sid !== "string" || sid !== state.sessionId) return state; // stale: never changes anything
  switch (detail.name) {
    case "stt:partial": {
      const text = typeof detail.payload.text === "string" ? detail.payload.text : "";
      const isFinal = detail.payload.isFinal === true;
      return {
        ...state,
        entries: updateLive(state, sid, { question: text, questionFinal: isFinal }),
      };
    }
    case "llm:done": {
      const answer = typeof detail.payload.answer === "string" ? detail.payload.answer : "";
      const transcript =
        typeof detail.payload.transcript === "string" ? detail.payload.transcript : "";
      const metrics = (detail.payload.metrics ?? null) as Metrics | null;
      const entries = state.entries.map((entry) =>
        entry.live && entry.id === sid
          ? {
              ...entry,
              answer,
              question: transcript,
              questionFinal: true,
              metrics,
              live: false,
            }
          : entry,
      );
      return { ...state, entries, phase: "idle", sessionId: null, done: true, rms: 0 };
    }
    case "session:error": {
      const error = detail.payload.error as AppErrorPayload | undefined;
      if (!error || error.code === "aborted") return state; // aborted is always silent
      const retired = retireLive(state);
      return {
        ...state,
        ...retired,
        phase: "idle",
        sessionId: null,
        error,
        rms: 0,
      };
    }
    case "audio:level": {
      const rms = typeof detail.payload.rms === "number" ? detail.payload.rms : 0;
      return { ...state, rms };
    }
    case "session:autostopped":
      return { ...state, phase: "finalizing", capped: true };
    default:
      return state;
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reduce, initialState);
  const [askText, setAskText] = useState("");
  const stateRef = useRef(state);
  stateRef.current = state;

  const trackedRef = useRef<string | null>(null);
  const callInFlightRef = useRef(false);
  const abortStartRef = useRef(false);
  const bufferRef = useRef<AppEventDetail[]>([]);
  const deltaBufRef = useRef<{ sid: string; parts: string[] } | null>(null);
  const flushScheduledRef = useRef(false);
  const gearRef = useRef<HTMLButtonElement>(null);
  const recordRef = useRef<HTMLButtonElement>(null);

  const flushDeltas = useCallback(() => {
    flushScheduledRef.current = false;
    const buf = deltaBufRef.current;
    if (!buf || buf.parts.length === 0) return;
    deltaBufRef.current = null;
    dispatch({ type: "deltas", sid: buf.sid, text: buf.parts.join("") });
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushScheduledRef.current) return;
    flushScheduledRef.current = true;
    // Coalesced to one paint per animation frame while streaming.
    const raf =
      typeof window.requestAnimationFrame === "function"
        ? window.requestAnimationFrame.bind(window)
        : (cb: FrameRequestCallback) => window.setTimeout(() => cb(0), 16);
    raf(() => flushDeltas());
  }, [flushDeltas]);

  const processEvent = useCallback(
    (detail: AppEventDetail) => {
      if (detail.name === "llm:delta") {
        const sid = detail.payload.sessionId;
        const delta = detail.payload.delta;
        if (typeof sid !== "string" || typeof delta !== "string") return;
        if (sid !== trackedRef.current) return;
        const buf = deltaBufRef.current;
        if (buf && buf.sid === sid) buf.parts.push(delta);
        else deltaBufRef.current = { sid, parts: [delta] };
        scheduleFlush();
        return;
      }
      if (detail.name === "llm:done" || detail.name === "session:error") {
        flushDeltas(); // deltas must land before the terminal event
      }
      dispatch({ type: "event", detail });
      if (
        (detail.name === "llm:done" || detail.name === "session:error") &&
        detail.payload.sessionId === trackedRef.current
      ) {
        trackedRef.current = null;
      }
    },
    [flushDeltas, scheduleFlush],
  );

  const adoptSession = useCallback(
    (sid: string, accept: Action) => {
      trackedRef.current = sid;
      // The accept action must reach the reducer BEFORE the replayed events,
      // or the reducer drops them as stale (its sessionId is not set yet).
      // React processes queued dispatches in order, so this sequencing holds.
      dispatch(accept);
      const buffered = bufferRef.current;
      bufferRef.current = [];
      for (const detail of buffered) {
        if (detail.payload.sessionId === sid) processEvent(detail);
      }
    },
    [processEvent],
  );

  useEffect(() => {
    const unsubscribe = subscribeAppEvents((detail) => {
      if (detail.name === "hotkey:toggle") {
        if (!stateRef.current.settingsOpen) onRecordToggleRef.current();
        return;
      }
      const sid = detail.payload.sessionId;
      if (typeof sid !== "string") return;
      if (sid === trackedRef.current) {
        processEvent(detail);
      } else if (callInFlightRef.current) {
        // The start/ask promise hasn't resolved yet: hold, replay on adopt.
        bufferRef.current.push(detail);
      }
      // Otherwise: stale id — dropped, changes nothing, ever.
    });
    const stopBeat = startHeartbeat();
    return () => {
      unsubscribe();
      stopBeat();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processEvent]);

  useEffect(() => {
    void bridge.getSettings().then((result) => {
      if (result.ok) dispatch({ type: "settings-loaded", view: result.value });
    });
  }, []);

  useEffect(() => {
    if (state.phase !== "recording") return;
    const id = window.setInterval(() => dispatch({ type: "tick" }), 1000);
    return () => window.clearInterval(id);
  }, [state.phase]);

  const doStart = useCallback(async () => {
    dispatch({ type: "start-pressed" });
    abortStartRef.current = false;
    callInFlightRef.current = true;
    const result = await bridge.startSession();
    callInFlightRef.current = false;
    if (!result.ok) {
      bufferRef.current = [];
      // `aborted` means a newer command superseded this one — always silent.
      if (result.error.code === "aborted") dispatch({ type: "start-aborted" });
      else dispatch({ type: "start-failed", error: result.error });
      return;
    }
    if (abortStartRef.current) {
      void bridge.cancelSession(result.value);
      bufferRef.current = [];
      return; // UI already went idle
    }
    adoptSession(result.value, { type: "start-accepted", sid: result.value });
  }, [adoptSession]);

  const doStop = useCallback(async () => {
    const sid = trackedRef.current;
    if (!sid) return;
    dispatch({ type: "stop-accepted" });
    const result = await bridge.stopSession(sid);
    if (!result.ok) {
      // Not taken: the session is connecting or already gone. Recover.
      void bridge.cancelSession(sid);
      trackedRef.current = null;
      dispatch({ type: "stop-not-taken" });
    }
  }, []);

  const onRecordToggle = useCallback(() => {
    const phase = stateRef.current.phase;
    if (phase === "recording") {
      void doStop();
    } else if (phase === "starting") {
      // Abort the pending start: silent teardown.
      abortStartRef.current = true;
      const sid = trackedRef.current;
      trackedRef.current = null;
      if (sid) void bridge.cancelSession(sid);
      dispatch({ type: "start-aborted" });
    } else if (phase === "idle" || phase === "answering") {
      void doStart();
    }
    // finalizing: ignore — the answer is about to stream.
  }, [doStart, doStop]);
  const onRecordToggleRef = useRef(onRecordToggle);
  onRecordToggleRef.current = onRecordToggle;

  const submitAsk = useCallback(
    async (text: string) => {
      const phase = stateRef.current.phase;
      if (phase !== "idle" && phase !== "answering") return; // belt and braces
      const trimmed = text.trim();
      if (!trimmed) return; // empty submits never reach the core
      callInFlightRef.current = true;
      const result = await bridge.ask(trimmed);
      callInFlightRef.current = false;
      if (!result.ok) {
        bufferRef.current = [];
        // `aborted` is silent (superseded); the input stays either way so the
        // user can retry.
        if (result.error.code !== "aborted") {
          dispatch({ type: "error-set", error: result.error });
        }
        return;
      }
      adoptSession(result.value, {
        type: "ask-accepted",
        sid: result.value,
        text: trimmed,
      });
      setAskText(""); // cleared only when the ask was accepted
    },
    [adoptSession],
  );

  const onStyleSelect = useCallback(async (style: AnswerStyle) => {
    const result = await bridge.setSettings({ answerStyle: style });
    if (result.ok) dispatch({ type: "settings-loaded", view: result.value });
    else dispatch({ type: "error-set", error: result.error });
  }, []);

  const openSettings = useCallback(() => dispatch({ type: "settings-open" }), []);
  const focusGearOnCloseRef = useRef(false);
  const closeSettings = useCallback(() => {
    // The gear is unmounted while Settings is open; focus it AFTER the main
    // view re-renders, not now (gearRef.current is null at this instant).
    focusGearOnCloseRef.current = true;
    dispatch({ type: "settings-close" });
  }, []);

  useEffect(() => {
    if (!state.settingsOpen && focusGearOnCloseRef.current) {
      focusGearOnCloseRef.current = false;
      gearRef.current?.focus();
    }
  }, [state.settingsOpen]);

  const clearHistory = useCallback(() => {
    dispatch({ type: "history-clear" });
    recordRef.current?.focus(); // the button focus lived on disappears
  }, []);

  const settings = state.settings;
  const hotkeyLabel = settings?.hotkey ? formatHotkey(settings.hotkey) : "";
  const hotkeyOn = settings?.hotkeyRegistered === true;
  const viewed: Entry | null = state.entries[state.view] ?? null;
  const viewingLive = viewed?.live === true;

  const missingKeys =
    settings !== null &&
    (!hasKey(settings, "deepgram") || !hasKey(settings, settings.llmProvider));

  let status: string;
  if (state.phase === "starting") status = "Opening the microphone feed…";
  else if (state.phase === "recording") status = "Recording call audio…";
  else if (state.phase === "finalizing")
    status = state.capped ? "Reached the 120s limit — answering now" : "Finalizing transcript…";
  else if (state.phase === "answering") status = "Generating answer…";
  else if (missingKeys)
    status = "First run: open Settings (gear icon) and add your API keys";
  else if (state.done) status = "Done — press Record for the next question";
  else
    status =
      "Ready — press Record while the other person is speaking" +
      (hotkeyOn && hotkeyLabel ? ` or ${hotkeyLabel}` : "");

  const recordLabel =
    state.phase === "starting"
      ? "Starting…"
      : state.phase === "recording"
        ? "Stop & Answer"
        : "Record";

  const askDisabled =
    state.phase === "starting" ||
    state.phase === "recording" ||
    state.phase === "finalizing";

  const canRegenerate =
    viewed !== null &&
    viewed.question.trim() !== "" &&
    (state.phase === "idle" || state.phase === "answering");

  if (state.settingsOpen && settings) {
    return (
      <SettingsPanel
        settings={settings}
        onSaved={(view) => dispatch({ type: "settings-loaded", view })}
        onBack={closeSettings}
      />
    );
  }

  return (
    <div className="app">
      <header className="header">
        <span
          className={`status-dot ${state.phase !== "idle" ? "status-dot-active" : ""}`}
          aria-hidden="true"
        />
        <h1 className="title">AI Call Assistant</h1>
        <button
          ref={gearRef}
          type="button"
          className="icon-button"
          aria-label="Settings"
          onClick={openSettings}
        >
          ⚙
        </button>
      </header>

      <p className="status-line" role="status">
        {status}
      </p>

      <div className="record-row">
        <button
          ref={recordRef}
          type="button"
          className={`record-button record-${state.phase}`}
          onClick={onRecordToggle}
        >
          {recordLabel}
        </button>
        {hotkeyOn && hotkeyLabel && <span className="hotkey-chip">{hotkeyLabel}</span>}
      </div>
      {settings !== null && settings.hotkey !== "" && !hotkeyOn && (
        <p className="hotkey-taken">
          {hotkeyLabel} is already taken by another app, so the shortcut is off —
          record from this window, or pick a different one in Settings.
        </p>
      )}

      {state.phase === "recording" && (
        <div className="meter-row">
          <div
            className="level-meter"
            role="img"
            aria-label="Audio level"
            data-testid="level-meter"
          >
            <div
              className="level-fill"
              style={{ width: `${Math.min(100, Math.round(state.rms * 140))}%` }}
            />
          </div>
          <span className="timer">{formatMmSs(state.seconds)}</span>
        </div>
      )}

      <form
        className="ask-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submitAsk(askText);
        }}
      >
        <input
          type="text"
          className="ask-input"
          placeholder="Type a question instead…"
          aria-label="Type a question"
          value={askText}
          disabled={askDisabled}
          onChange={(event) => setAskText(event.target.value)}
        />
        <button type="submit" className="ask-button" disabled={askDisabled}>
          Ask
        </button>
      </form>

      <div className="style-chips" role="group" aria-label="Answer style">
        {(["brief", "balanced", "detailed"] as const).map((style) => (
          <button
            key={style}
            type="button"
            className="style-chip"
            aria-pressed={settings?.answerStyle === style}
            onClick={() => void onStyleSelect(style)}
          >
            {style.charAt(0).toUpperCase() + style.slice(1)}
          </button>
        ))}
      </div>

      <section className="panel" aria-label="Question heard">
        <div className="panel-title">
          <h2>Question heard</h2>
          {state.phase === "recording" && viewingLive && (
            <span className="tag tag-live">live</span>
          )}
        </div>
        <div className="panel-body transcript-body">
          {viewed && viewed.question ? (
            viewed.question
          ) : state.phase === "recording" && viewingLive ? (
            <span className="placeholder">Listening…</span>
          ) : (
            <span className="placeholder">
              The live transcript will appear here while you record.
            </span>
          )}
        </div>
      </section>

      <AnswerPanel
        entry={viewed}
        answering={state.phase === "answering" && viewingLive}
        canRegenerate={canRegenerate}
        onRegenerate={() => {
          if (viewed) void submitAsk(viewed.question);
        }}
        onCopyError={(message) =>
          dispatch({ type: "error-set", error: { code: "internal", message } })
        }
        onAnnounce={(text) => dispatch({ type: "announce", text })}
        // Entry identity, not the view index: with a full history the index
        // stays put while the entry underneath it changes, and scroll would
        // never reset.
        viewKey={viewed?.id ?? ""}
      />

      {state.error && (
        <div className="error-box" role="alert">
          {state.error.message}
        </div>
      )}

      <HistoryBar
        count={state.entries.length}
        index={state.view}
        canClear={state.phase === "idle"}
        onPrev={() => dispatch({ type: "history-nav", delta: -1 })}
        onNext={() => dispatch({ type: "history-nav", delta: 1 })}
        onClear={clearHistory}
      />

      <span className="visually-hidden" role="status">
        {state.announcement}
      </span>
    </div>
  );
}
