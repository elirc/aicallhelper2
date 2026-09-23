/**
 * The pure UI state machine: State, Action, and the reducer.
 *
 * No React, no bridge, no timers in here — App.tsx owns the plumbing (event
 * subscription, delta coalescing, the in-flight buffer) and dispatches into
 * this reducer. Keeping it pure is what makes every phase transition
 * testable without a DOM.
 */
import type {
  AppErrorPayload,
  AppEventDetail,
  CallType,
  Metrics,
  CoreState,
  ProtectionVerdict,
  SettingsView,
  StatusSnapshot,
} from "./types";

export type Phase = "idle" | "starting" | "recording" | "finalizing" | "answering";

export interface Entry {
  id: string;
  question: string;
  questionFinal: boolean;
  answer: string;
  metrics: Metrics | null;
  /** The call type the answer was generated under (from llm:done). */
  callType: CallType | null;
  /** How the provider ended the answer (llm:done `finish`); absent = complete. */
  finish: AnswerFinish;
  live: boolean;
}

export type AnswerFinish = "complete" | "truncated" | "refused";

/** The inline notice for an answer that did not finish normally, or null. */
export function finishNotice(finish: AnswerFinish | undefined): string | null {
  if (finish === "truncated") {
    return "Answer cut off at the length limit — press Regenerate or ask for a shorter answer.";
  }
  if (finish === "refused") return "The model declined to finish this answer.";
  return null;
}

export interface State {
  phase: Phase;
  sessionId: string | null;
  entries: Entry[];
  view: number;
  error: AppErrorPayload | null;
  rms: number;
  /** Recording second at which audio was last heard, or null if never.
   *  Loopback silence is the most common real-world failure (audio routed to
   *  a headset, the wrong output device, a muted call) and it can also start
   *  mid-recording when a device is unplugged or Windows switches the default
   *  output — tracking WHEN, not merely whether, catches both. */
  heardAudioAt: number | null;
  /** Tri-state: "unknown" until a verdict (event or snapshot) arrives. */
  protection: ProtectionVerdict;
  /** Highest core status revision adopted; -1 = none yet. An event or
   *  snapshot with a LOWER revision is older news and is ignored. */
  statusRevision: number;
  /** Core build state; "unknown" until an event or snapshot says. */
  core: CoreState | "unknown";
  coreError: string | null;
  /** Revision of the last adopted core state (same counter, per field). */
  coreRevision: number;
  seconds: number;
  /** The core's recording-cap deadline (epoch ms, `session:recording`), or
   *  null when the core did not send one — then the local tick is used. */
  deadlineMs: number | null;
  capped: boolean;
  /** A stop was refused; waiting to see whether an answer still arrives. */
  stopStranded: boolean;
  done: boolean;
  settingsOpen: boolean;
  settings: SettingsView | null;
  announcement: string;
}

export const MAX_HISTORY = 6;
// Above digital silence and typical comfort noise, below any real speech.
export const AUDIBLE_RMS = 0.003;
// Long enough that a natural pause in the question never trips it.
export const SILENCE_HINT_AFTER_S = 5;
// Long enough for a finalize the 120 s cap already started (5 s STT cap
// plus the answer's first token), short enough not to feel stuck.
export const STOP_RECOVERY_MS = 20_000;
// The core's recording hard cap; the countdown in the UI mirrors it.
export const RECORD_CAP_S = 120;

export const initialState: State = {
  phase: "idle",
  sessionId: null,
  entries: [],
  view: 0,
  error: null,
  rms: 0,
  heardAudioAt: null,
  protection: "unknown",
  statusRevision: -1,
  core: "unknown",
  coreError: null,
  coreRevision: -1,
  seconds: 0,
  deadlineMs: null,
  capped: false,
  stopStranded: false,
  done: false,
  settingsOpen: false,
  settings: null,
  announcement: "",
};

export type Action =
  | { type: "settings-loaded"; view: SettingsView }
  | { type: "start-pressed" }
  | { type: "start-accepted"; sid: string }
  | { type: "start-failed"; error: AppErrorPayload }
  | { type: "start-aborted" }
  | { type: "stop-accepted" }
  | { type: "stop-not-taken" }
  | { type: "stop-recover"; sid: string }
  | { type: "ask-accepted"; sid: string; text: string }
  /** A renderer reload found a live core session: track it again. */
  | { type: "session-resumed"; sid: string; phase: "recording" | "finalizing" | "answering" }
  | { type: "status-snapshot"; snapshot: StatusSnapshot }
  | { type: "error-set"; error: AppErrorPayload }
  | { type: "error-dismiss" }
  | { type: "deltas"; sid: string; text: string }
  | { type: "event"; detail: AppEventDetail }
  | { type: "tick" }
  | { type: "settings-open" }
  | { type: "settings-close" }
  | { type: "history-nav"; delta: number }
  | { type: "history-clear" }
  | { type: "announce"; text: string }
  | { type: "protection"; verdict: ProtectionVerdict; revision?: number }
  | { type: "core"; core: CoreState; error: string | null; revision?: number };

function adoptCore(
  state: State,
  core: CoreState,
  error: string | null,
  revision: number | undefined,
): State {
  if (typeof revision === "number") {
    if (revision < state.coreRevision) return state;
    return { ...state, core, coreError: error, coreRevision: revision };
  }
  return { ...state, core, coreError: error };
}

/** Adopt a protection verdict only if it is not older than what we have. */
function adoptVerdict(
  state: State,
  verdict: ProtectionVerdict,
  revision: number | undefined,
): State {
  if (typeof revision === "number") {
    if (revision < state.statusRevision) return state; // stale: a fresher verdict won
    return { ...state, protection: verdict, statusRevision: revision };
  }
  return { ...state, protection: verdict };
}

/** Retire the live entry into history if it captured user work; discard an
 * attempt that captured nothing (whitespace-only counts as nothing). */
export function retireLive(state: State): Pick<State, "entries" | "view"> {
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

export function pushLive(state: State, entry: Entry): Pick<State, "entries" | "view"> {
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

function newEntry(sid: string, question: string, questionFinal: boolean): Entry {
  return {
    id: sid,
    question,
    questionFinal,
    answer: "",
    metrics: null,
    callType: null,
    finish: "complete",
    live: true,
  };
}

export function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "settings-loaded":
      return { ...state, settings: action.view };
    case "start-pressed":
      return {
        ...state,
        phase: "starting",
        error: null,
        done: false,
        capped: false,
        stopStranded: false,
      };
    case "start-accepted": {
      const placed = pushLive(state, newEntry(action.sid, "", false));
      return {
        ...state,
        ...placed,
        phase: "recording",
        sessionId: action.sid,
        seconds: 0,
        deadlineMs: null,
        rms: 0,
        heardAudioAt: null,
        stopStranded: false,
      };
    }
    // App.tsx dispatches these two only for the LATEST start command; a stale
    // response never reaches the reducer (R04). Both also only act while the
    // UI is still "starting", so nothing can knock over an adopted session.
    case "start-failed":
      // The failure is still worth reporting, but only a "starting" UI goes
      // idle; a phase some other event already moved on is left alone.
      return {
        ...state,
        phase: state.phase === "starting" ? "idle" : state.phase,
        error: action.error,
      };
    case "start-aborted":
      if (state.phase !== "starting") return state;
      return { ...state, phase: "idle", sessionId: null };
    case "stop-accepted":
      return { ...state, phase: "finalizing" };
    case "stop-not-taken":
      // Stay put and keep tracking: the session may be mid-finalize from the
      // 120 s cap, in which case its answer is still coming. `stopStranded`
      // arms the bounded fallback for the case where it really is gone.
      return { ...state, phase: "finalizing", stopStranded: true };
    case "stop-recover": {
      // Nothing arrived after a refused stop, so the session really was gone.
      // Session-scoped: a timer armed for an older session must never touch
      // a newer one, and progress since then (which clears the flag) wins.
      if (!state.stopStranded || state.sessionId !== action.sid) return state;
      const retired = retireLive(state);
      return {
        ...state,
        ...retired,
        phase: "idle",
        sessionId: null,
        stopStranded: false,
      };
    }
    case "ask-accepted": {
      const placed = pushLive(state, newEntry(action.sid, action.text, true));
      return {
        ...state,
        ...placed,
        phase: "answering",
        sessionId: action.sid,
        error: null,
        done: false,
        capped: false,
        stopStranded: false, // a replacement session was never stranded
      };
    }
    case "session-resumed": {
      if (state.sessionId !== null || state.phase !== "idle") return state;
      const placed = pushLive(state, newEntry(action.sid, "", false));
      return {
        ...state,
        ...placed,
        phase: action.phase,
        sessionId: action.sid,
        seconds: 0,
        deadlineMs: null,
        rms: 0,
        heardAudioAt: null,
        stopStranded: false,
      };
    }
    case "status-snapshot": {
      const snap = action.snapshot;
      const next = adoptVerdict(state, snap.protection, snap.revision);
      return snap.core
        ? adoptCore(next, snap.core, snap.coreError ?? null, snap.revision)
        : next;
    }
    case "core":
      return adoptCore(state, action.core, action.error, action.revision);
    case "error-set":
      return { ...state, error: action.error };
    case "error-dismiss":
      return { ...state, error: null };
    case "deltas": {
      if (state.sessionId !== action.sid) return state;
      const entries = state.entries.map((entry) =>
        entry.live && entry.id === action.sid
          ? { ...entry, answer: entry.answer + action.text }
          : entry,
      );
      // A valid delta proves the session is alive: disarm refused-stop
      // recovery, or it would retire a healthy streaming answer (R08).
      return { ...state, entries, phase: "answering", stopStranded: false };
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
    case "protection":
      return adoptVerdict(state, action.verdict, action.revision);
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
      const callType =
        typeof detail.payload.callType === "string"
          ? (detail.payload.callType as CallType)
          : null;
      const finish: AnswerFinish =
        detail.payload.finish === "truncated" || detail.payload.finish === "refused"
          ? detail.payload.finish
          : "complete";
      const entries = state.entries.map((entry) =>
        entry.live && entry.id === sid
          ? {
              ...entry,
              answer,
              question: transcript,
              questionFinal: true,
              metrics,
              callType,
              finish,
              live: false,
            }
          : entry,
      );
      return {
        ...state,
        entries,
        phase: "idle",
        sessionId: null,
        done: true,
        rms: 0,
        stopStranded: false,
      };
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
        stopStranded: false,
      };
    }
    case "audio:level": {
      const rms = typeof detail.payload.rms === "number" ? detail.payload.rms : 0;
      return {
        ...state,
        rms,
        heardAudioAt: rms > AUDIBLE_RMS ? state.seconds : state.heardAudioAt,
      };
    }
    case "session:recording": {
      // The core's ONE cap deadline, armed when capture actually started.
      const deadline = detail.payload.deadlineMs;
      if (typeof deadline !== "number" || !Number.isFinite(deadline)) return state;
      return { ...state, deadlineMs: deadline };
    }
    case "session:autostopped":
      return { ...state, phase: "finalizing", capped: true };
    default:
      return state;
  }
}

/** The one-line status the header shows for a phase. */
export function statusFor(
  state: State,
  options: { missingKeys: boolean; hotkeyLabel: string },
): string {
  // Loopback captures the system OUTPUT mix; no microphone is ever opened.
  if (state.phase === "starting") return "Starting system-audio capture…";
  if (state.phase === "recording") return "Recording call audio…";
  if (state.phase === "finalizing") {
    return state.capped ? "Reached the 120s limit — answering now" : "Finalizing transcript…";
  }
  if (state.phase === "answering") return "Generating answer…";
  if (options.missingKeys) return "First run: open Settings (gear icon) and add your API keys";
  if (state.done) return "Done — press Record for the next question";
  return (
    "Ready — press Record while the other person is speaking" +
    (options.hotkeyLabel ? ` or ${options.hotkeyLabel}` : "")
  );
}

/** The recovery notice for a settings file that could not be loaded, or null. */
export function settingsFileNotice(view: SettingsView | null): string | null {
  const file = view?.settingsFile;
  if (!file || (file.load !== "unreadable" && file.load !== "invalid")) return null;
  return (
    "Your settings file couldn't be read, so defaults are shown." +
    (file.backup ? ` A copy was saved as ${file.backup}.` : "")
  );
}

/** The terminal core-failure banner text, or null. */
export function coreFailedNotice(state: State): string | null {
  if (state.core !== "failed") return null;
  return (
    "The app core failed to start" +
    (state.coreError ? ` (${state.coreError})` : "") +
    ". Recording and answers are unavailable — restart the app."
  );
}

/** Seconds left before the core's recording cap. Driven by the core's
 *  deadline when it sent one (a local tick starts at start-accepted, before
 *  the device opens, and drifts while the page is throttled); otherwise the
 *  local tick is the fallback. */
export function secondsLeft(state: State, nowMs: number): number {
  if (state.deadlineMs !== null) {
    return Math.max(0, Math.ceil((state.deadlineMs - nowMs) / 1000));
  }
  return Math.max(0, RECORD_CAP_S - state.seconds);
}

/** Fires both when nothing was ever heard and when audio stops for a
 * stretch, which is what a device dying mid-recording looks like. */
export function silentSoFar(state: State): boolean {
  return (
    state.phase === "recording" &&
    state.seconds - (state.heardAudioAt ?? 0) >= SILENCE_HINT_AFTER_S
  );
}
