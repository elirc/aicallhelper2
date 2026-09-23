import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { bridge, startHeartbeat, subscribeAppEvents } from "./bridge";
import { AnswerPanel } from "./components/AnswerPanel";
import { HistoryBar } from "./components/HistoryBar";
import { PrompterView } from "./components/PrompterView";
import { ProtectionNotice } from "./components/ProtectionNotice";
import { SettingsPanel } from "./components/SettingsPanel";
import { formatHotkey, formatMmSs } from "./format";
import {
  STOP_RECOVERY_MS,
  initialState,
  reduce,
  coreFailedNotice,
  secondsLeft,
  settingsFileNotice,
  silentSoFar,
  statusFor,
  type Action,
  type Entry,
} from "./state";
import type { AnswerStyle, AppEventDetail, CallType } from "./types";
import {
  ANSWER_FONT_MAX,
  ANSWER_FONT_MIN,
  ANSWER_STYLES,
  FONT_STEP,
  PROMPTER_FONT_MAX,
  PROMPTER_FONT_MIN,
  hasKey,
} from "./types";

export type { Entry, Phase } from "./state";

// The countdown appears for the last stretch before the core's hard cap.
const COUNTDOWN_LAST_S = 30;
// Retry cadence for a failed first settings load (core still starting).
const SETTINGS_RETRY_MS = 3000;
// How many times a refused-stop recovery defers to a core that still reports
// the session as finalizing/answering (x STOP_RECOVERY_MS), before giving up.
const STOP_RECHECK_LIMIT = 6;

interface PendingCommand {
  gen: number;
  kind: "start" | "ask";
  /** The user explicitly aborted this command (Record pressed while starting). */
  aborted: boolean;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** "Claude Haiku 4.5 (recommended)" -> "Claude Haiku 4.5" for the header chip. */
function shortProviderName(name: string): string {
  return name.replace(/\s*\([^)]*\)\s*$/, "");
}

export default function App() {
  const [state, dispatch] = useReducer(reduce, initialState);
  const [askText, setAskText] = useState("");
  const stateRef = useRef(state);
  stateRef.current = state;

  const trackedRef = useRef<string | null>(null);
  // Per-command generations (R04). Every start/ask takes a new generation;
  // only the response of the LATEST one may change UI state. A stale response
  // may clean up its own session (cancel it) but never touches state, never
  // clears the replay buffer, and never clears the newer command's in-flight
  // marker. `aborted` is carried on the command itself, so a later start can
  // no longer reset an earlier explicit abort.
  const cmdGenRef = useRef(0);
  const pendingRef = useRef<PendingCommand | null>(null);
  const bufferRef = useRef<AppEventDetail[]>([]);
  // Event ordering (R07): highest `seq` applied, and this page's generation
  // once the core told us (both optional in the payload — see CONTRACT).
  const lastSeqRef = useRef(Number.NEGATIVE_INFINITY);
  const pageGenRef = useRef<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [closeRequests, setCloseRequests] = useState(0);
  const deltaBufRef = useRef<{ sid: string; parts: string[] } | null>(null);
  const flushScheduledRef = useRef(false);
  const gearRef = useRef<HTMLButtonElement>(null);
  const recordRef = useRef<HTMLButtonElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);

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
      const payload = detail.payload;
      // An event dispatched to a previous page (a timed-out evaluate_js that
      // executed after a reload) belongs to a superseded page generation.
      if (
        typeof payload.pageGen === "number" &&
        pageGenRef.current !== null &&
        payload.pageGen < pageGenRef.current
      ) {
        return;
      }
      // A retried batch can re-deliver an event that already executed; an
      // abandoned call can land after newer ones. Neither may apply twice.
      if (typeof payload.seq === "number") {
        if (payload.seq <= lastSeqRef.current) return;
        lastSeqRef.current = payload.seq;
      }
      if (detail.name === "hotkey:toggle") {
        // Settings hides the recording controls, but capture keeps running
        // behind it: the shortcut must still be able to STOP (or abort a
        // pending start). It never starts a recording the user cannot see.
        const s = stateRef.current;
        if (!s.settingsOpen || s.phase === "recording" || s.phase === "starting") {
          onRecordToggleRef.current();
        }
        return;
      }
      if (detail.name === "core:ready" || detail.name === "core:failed") {
        dispatch({
          type: "core",
          core: detail.name === "core:ready" ? "ready" : "failed",
          error: typeof payload.error === "string" ? payload.error : null,
          revision: typeof payload.revision === "number" ? payload.revision : undefined,
        });
        return;
      }
      if (detail.name === "window:close-requested") {
        // The shell cancelled ONE native close because Settings holds unsaved
        // edits; the panel shows its save/discard choice.
        setCloseRequests((n) => n + 1);
        return;
      }
      if (detail.name === "protection:failed" || detail.name === "protection:ok") {
        dispatch({
          type: "protection",
          verdict: detail.name === "protection:failed" ? "unprotected" : "protected",
          revision: typeof payload.revision === "number" ? payload.revision : undefined,
        });
        return;
      }
      const sid = payload.sessionId;
      if (typeof sid !== "string") return;
      if (sid === trackedRef.current) {
        processEvent(detail);
      } else if (pendingRef.current) {
        // A start/ask promise hasn't resolved yet: hold, replay on adopt.
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
    // A failed first load used to leave settings null for the life of the
    // page with no message (R11). Say so, and keep retrying: the core may
    // simply still be starting.
    let alive = true;
    let timer: number | undefined;
    const load = () => {
      void bridge.getSettings().then((result) => {
        if (!alive) return;
        if (result.ok) {
          setLoadError(null);
          dispatch({ type: "settings-loaded", view: result.value });
        } else {
          setLoadError(result.error.message);
          timer = window.setTimeout(load, SETTINGS_RETRY_MS);
        }
      });
    };
    load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    // Every page load (including a watchdog reload) asks the core for the
    // authoritative snapshot, so a protection verdict emitted before this
    // page subscribed is never lost, and a session still running behind a
    // reloaded page is tracked again instead of orphaned (R01/R11).
    let alive = true;
    void bridge.getStatus().then((result) => {
      if (!alive || !result.ok) return;
      const snap = result.value;
      if (typeof snap.pageGeneration === "number") {
        pageGenRef.current = Math.max(pageGenRef.current ?? snap.pageGeneration, snap.pageGeneration);
      }
      const s = stateRef.current;
      // The reducer adopts each field only if the snapshot is not older news.
      dispatch({ type: "status-snapshot", snapshot: snap });
      if (snap.revision < s.statusRevision) return; // too old to resume from
      const phase = snap.session.phase;
      if (
        snap.session.id !== null &&
        (phase === "recording" || phase === "finalizing" || phase === "answering") &&
        s.phase === "idle" &&
        trackedRef.current === null &&
        pendingRef.current === null
      ) {
        trackedRef.current = snap.session.id;
        dispatch({ type: "session-resumed", sid: snap.session.id, phase });
      }
    });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    // A refused stop leaves us waiting on an answer that may or may not be
    // coming. Give it a bounded grace period rather than either hanging in
    // "Finalizing…" forever or destroying a live session outright. The timer
    // is scoped to the session it was armed for (R08): progress clears the
    // flag, and a replacement session re-keys this effect.
    const sid = state.sessionId;
    if (!state.stopStranded || sid === null) return;
    let cancelled = false;
    let rechecks = 0;
    let id: number | undefined;
    const recover = () => {
      dispatch({ type: "stop-recover", sid });
      // UI and core must agree: the core is told to drop it as well.
      void bridge.cancelSession(sid);
    };
    const arm = () => {
      id = window.setTimeout(() => {
        // Prefer the core's word over silence. Without a status command the
        // fallback is immediate and unchanged; a failed or slow query also
        // falls back, and a session the core no longer reports as live is
        // never revived.
        if (!bridge.hasStatus()) {
          recover();
          return;
        }
        void bridge.getStatus().then((result) => {
          if (cancelled) return;
          // "unknown" means the core loop did not answer in time: it cannot
          // say the session is dead, so wait again rather than cancel it.
          const live =
            result.ok &&
            (result.value.session.phase === "unknown" ||
              (result.value.session.id === sid &&
                (result.value.session.phase === "finalizing" ||
                  result.value.session.phase === "answering")));
          if (live && rechecks < STOP_RECHECK_LIMIT) {
            rechecks += 1;
            arm();
          } else {
            recover();
          }
        });
      }, STOP_RECOVERY_MS);
    };
    arm();
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [state.stopStranded, state.sessionId]);

  useEffect(() => {
    if (state.phase !== "recording") return;
    const id = window.setInterval(() => dispatch({ type: "tick" }), 1000);
    return () => window.clearInterval(id);
  }, [state.phase]);

  /** Claim a new command generation; it supersedes any in-flight one. */
  const beginCommand = useCallback((kind: PendingCommand["kind"]): PendingCommand => {
    cmdGenRef.current += 1;
    const cmd: PendingCommand = { gen: cmdGenRef.current, kind, aborted: false };
    pendingRef.current = cmd;
    return cmd;
  }, []);

  /** Settle a command's response. Returns true only for the LATEST command
   *  that the user has not aborted — the only one allowed to touch state. A
   *  stale or aborted command that nonetheless created a session cancels
   *  that session (its own, never the tracked one) and changes nothing else. */
  const settleCommand = useCallback(
    (cmd: PendingCommand, createdSid: string | null): boolean => {
      const latest = cmd.gen === cmdGenRef.current;
      if (pendingRef.current === cmd) pendingRef.current = null;
      if (latest && !cmd.aborted) return true;
      if (createdSid !== null && createdSid !== trackedRef.current) {
        void bridge.cancelSession(createdSid);
      }
      // The buffer is emptied only by the command it served: once no command
      // is in flight, anything left in it is orphaned.
      if (pendingRef.current === null) bufferRef.current = [];
      return false;
    },
    [],
  );

  const doStart = useCallback(async () => {
    dispatch({ type: "start-pressed" });
    const cmd = beginCommand("start");
    const result = await bridge.startSession();
    if (!settleCommand(cmd, result.ok ? result.value : null)) return;
    if (!result.ok) {
      bufferRef.current = [];
      // `aborted` means a newer command superseded this one — always silent.
      if (result.error.code === "aborted") dispatch({ type: "start-aborted" });
      else dispatch({ type: "start-failed", error: result.error });
      return;
    }
    adoptSession(result.value, { type: "start-accepted", sid: result.value });
  }, [adoptSession, beginCommand, settleCommand]);

  const doStop = useCallback(async () => {
    const sid = trackedRef.current;
    if (!sid) return;
    dispatch({ type: "stop-accepted" });
    const result = await bridge.stopSession(sid);
    // A response for a session we no longer track (superseded meanwhile)
    // must not strand the newer one.
    if (trackedRef.current !== sid) return;
    if (!result.ok) {
      // The core refused. Either the session really is gone, or it already
      // stopped ITSELF — the 120 s cap auto-stops and is finalizing right
      // now. Cancelling here would throw away the answer to a question the
      // user just spent two minutes asking, so never do that: keep waiting
      // and let the terminal event land. If none is coming, the timer below
      // recovers us instead of sitting in "Finalizing…" forever.
      dispatch({ type: "stop-not-taken" });
    }
  }, []);

  const onRecordToggle = useCallback(() => {
    const phase = stateRef.current.phase;
    if (phase === "recording") {
      void doStop();
    } else if (phase === "starting") {
      // Abort the pending start: silent teardown. The flag lives on THIS
      // command, so a later start cannot un-abort it.
      const pending = pendingRef.current;
      if (pending && pending.kind === "start") pending.aborted = true;
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
      const cmd = beginCommand("ask");
      const result = await bridge.ask(trimmed);
      if (!settleCommand(cmd, result.ok ? result.value : null)) return;
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
    [adoptSession, beginCommand, settleCommand],
  );

  // Every quick setting goes through one path and renders the PERSISTED
  // value from the returned view, never the clicked one.
  // Quick patches are SERIALIZED: separate bridge calls can reach the
  // core's settings lock in either order, so two fast clicks could commit
  // out of order. Chained, each patch lands (and is adopted) in click order.
  const patchQueueRef = useRef<Promise<void>>(Promise.resolve());
  const patchSettings = useCallback(
    (patch: Parameters<typeof bridge.setSettings>[0]): Promise<void> => {
      const run = async () => {
        const result = await bridge.setSettings(patch);
        if (result.ok) dispatch({ type: "settings-loaded", view: result.value });
        else dispatch({ type: "error-set", error: result.error });
      };
      const next = patchQueueRef.current.then(run, run);
      patchQueueRef.current = next;
      return next;
    },
    [],
  );
  // The font size the user last ASKED for, while its patch is in flight, so
  // two fast clicks step twice instead of both stepping from the same value.
  const fontRequestRef = useRef<{ answerFontPx?: number; prompterFontPx?: number }>({});
  const stepFont = useCallback(
    (field: "answerFontPx" | "prompterFontPx", delta: number, min: number, max: number) => {
      const current = fontRequestRef.current[field] ?? stateRef.current.settings?.[field];
      if (typeof current !== "number") return;
      const next = clamp(current + delta * FONT_STEP, min, max);
      if (next === current) return;
      fontRequestRef.current[field] = next;
      void patchSettings({ [field]: next }).then(() => {
        if (fontRequestRef.current[field] === next) delete fontRequestRef.current[field];
      });
    },
    [patchSettings],
  );

  const onStyleSelect = useCallback(
    (style: AnswerStyle) => void patchSettings({ answerStyle: style }),
    [patchSettings],
  );
  const onProfileSelect = useCallback(
    (id: string) => void patchSettings({ activeProfileId: id }),
    [patchSettings],
  );
  const onCallTypeSelect = useCallback(
    (callType: CallType) => void patchSettings({ callType }),
    [patchSettings],
  );
  const onAnswerFontStep = useCallback(
    (delta: number) => stepFont("answerFontPx", delta, ANSWER_FONT_MIN, ANSWER_FONT_MAX),
    [stepFont],
  );
  const onPrompterFontStep = useCallback(
    (delta: number) =>
      stepFont("prompterFontPx", delta, PROMPTER_FONT_MIN, PROMPTER_FONT_MAX),
    [stepFont],
  );
  const enterPrompter = useCallback(
    () => void patchSettings({ layoutMode: "prompter" }),
    [patchSettings],
  );
  const exitPrompter = useCallback(
    () => void patchSettings({ layoutMode: "full" }),
    [patchSettings],
  );
  const dockWindow = useCallback(() => void bridge.dockWindow(), []);
  const dismissError = useCallback(() => dispatch({ type: "error-dismiss" }), []);

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

  // The live transcript follows the newest words: the question's tail is
  // what the user is reacting to, and a capped box otherwise hides it.
  useEffect(() => {
    const el = transcriptRef.current;
    if (el && state.phase === "recording") el.scrollTop = el.scrollHeight;
  }, [viewed?.question, state.phase]);

  const missingKeys =
    settings !== null &&
    (!hasKey(settings, "deepgram") || !hasKey(settings, settings.llmProvider));

  const status = statusFor(state, {
    missingKeys,
    hotkeyLabel: hotkeyOn && hotkeyLabel ? hotkeyLabel : "",
  });

  const recordLabel =
    state.phase === "starting"
      ? "Starting…"
      : state.phase === "recording"
        ? "Stop & Answer"
        : state.phase === "finalizing"
          ? "Finalizing…" // a click here is ignored; don't pretend it records
          : "Record";

  const askDisabled =
    state.phase === "starting" ||
    state.phase === "recording" ||
    state.phase === "finalizing";

  const silent = silentSoFar(state);
  // Re-evaluated on every render; the 1 s tick re-renders while recording.
  const left = secondsLeft(state, Date.now());

  const canRegenerate =
    viewed !== null &&
    viewed.question.trim() !== "" &&
    (state.phase === "idle" || state.phase === "answering");

  const providerName = settings
    ? shortProviderName(
        settings.providers.find((p) => p.id === settings.llmProvider)?.displayName ??
          settings.llmProvider,
      )
    : "";
  const activeProfile = settings
    ? settings.profiles.find((p) => p.id === settings.activeProfileId) ?? null
    : null;
  const callTypeLabelOf = (id: CallType | null): string | null =>
    id ? (settings?.callTypes.find((c) => c.id === id)?.label ?? null) : null;

  const coreFailed = coreFailedNotice(state);
  const fileNotice = settingsFileNotice(settings);

  if (state.settingsOpen && settings) {
    return (
      <SettingsPanel
        settings={settings}
        onSaved={(view) => dispatch({ type: "settings-loaded", view })}
        onBack={closeSettings}
        protection={state.protection}
        phase={state.phase}
        status={status}
        onStop={onRecordToggle}
        closeRequests={closeRequests}
        coreFailed={coreFailed}
      />
    );
  }

  if (settings?.layoutMode === "prompter") {
    return (
      <PrompterView
        entry={viewed}
        phase={state.phase}
        seconds={state.seconds}
        recordLabel={recordLabel}
        answering={state.phase === "answering" && viewingLive}
        error={state.error}
        protection={state.protection}
        status={status}
        silent={silent}
        answerStyle={settings.answerStyle}
        fontPx={settings.prompterFontPx}
        historyCount={state.entries.length}
        historyIndex={state.view}
        onRecordToggle={onRecordToggle}
        onStyleSelect={onStyleSelect}
        onPrev={() => dispatch({ type: "history-nav", delta: -1 })}
        onNext={() => dispatch({ type: "history-nav", delta: 1 })}
        onFontStep={onPrompterFontStep}
        onDock={dockWindow}
        onExit={exitPrompter}
        onDismissError={dismissError}
        coreFailed={coreFailed}
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
        {settings && (
          <span
            className="header-chip"
            title={
              `Answers from ${providerName}` +
              (activeProfile ? ` · profile “${activeProfile.name}”` : "") +
              (activeProfile && activeProfile.resume.trim() === ""
                ? " · no resume saved"
                : "")
            }
          >
            {providerName}
            {settings.profiles.length > 1 && activeProfile ? ` · ${activeProfile.name}` : ""}
          </span>
        )}
        <button
          type="button"
          className="icon-button"
          aria-label="Enter prompter mode"
          title="Prompter: a wide strip under your camera with only the answer"
          onClick={enterPrompter}
          disabled={!settings}
        >
          ⤒
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label="Dock under camera"
          title="Move this window to the top-centre of the screen"
          onClick={dockWindow}
        >
          ⊤
        </button>
        <button
          ref={gearRef}
          type="button"
          className="icon-button"
          aria-label="Settings"
          title="Settings"
          onClick={openSettings}
        >
          ⚙
        </button>
      </header>

      <p className="status-line" role="status">
        {status}
      </p>

      <ProtectionNotice verdict={state.protection} />

      {loadError !== null && settings === null && (
        <p className="protection-warning" role="alert">
          Settings could not be loaded ({loadError}). Retrying…
        </p>
      )}

      {coreFailed && (
        <p className="protection-warning" role="alert">
          {coreFailed}
        </p>
      )}

      {fileNotice && (
        <p className="protection-warning" role="status">
          {fileNotice}
        </p>
      )}

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
        fontPx={settings ? settings.answerFontPx : null}
        onFontStep={onAnswerFontStep}
        callTypeLabel={callTypeLabelOf(viewed?.callType ?? null)}
      />

      <section className="panel" aria-label="Question heard">
        <div className="panel-title">
          <h2>Question heard</h2>
          {state.phase === "recording" && viewingLive && (
            <span className="tag tag-live">live</span>
          )}
        </div>
        <div ref={transcriptRef} className="panel-body transcript-body">
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

      <div className="record-row">
        <button
          ref={recordRef}
          type="button"
          className={`record-button record-${state.phase}`}
          onClick={onRecordToggle}
          aria-disabled={state.phase === "finalizing"}
          title={
            state.phase === "answering"
              ? "Start recording the next question (stops this answer)"
              : undefined
          }
        >
          {recordLabel}
        </button>
        {hotkeyOn && hotkeyLabel && (
          <span className="hotkey-chip" title="Global shortcut: toggles Record / Stop from any app">
            {hotkeyLabel}
          </span>
        )}
      </div>
      {settings !== null && settings.hotkey !== "" && !hotkeyOn && (
        <p className="hotkey-taken">
          {settings.hotkeyStatus === "invalid" ? (
            <>
              {hotkeyLabel} isn't a shortcut Windows understands, so it's off —
              try something like Ctrl+Shift+Space in Settings.
            </>
          ) : (
            <>
              {hotkeyLabel} is already taken by another app, so the shortcut is off —
              record from this window, or pick a different one in Settings.
            </>
          )}
        </p>
      )}

      {state.phase === "recording" && (
        <div className="meter-row">
          <div className="level-meter" aria-hidden="true" data-testid="level-meter">
            <div
              className="level-fill"
              style={{ width: `${Math.min(100, Math.round(state.rms * 140))}%` }}
            />
          </div>
          <span className="timer">{formatMmSs(state.seconds)}</span>
          {left <= COUNTDOWN_LAST_S && (
            <span className="timer-countdown" title="The recording stops itself at 2:00">
              {formatMmSs(left)} left
            </span>
          )}
        </div>
      )}
      {silent && (
        <p className="silence-hint" role="status">
          No call audio detected yet — check that the call is playing through
          your speakers, not a headset or another output device.
        </p>
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

      <div className="context-row">
        {settings && settings.profiles.length > 1 && (
          <select
            aria-label="Profile"
            title="Which profile (resume, job, notes) grounds the answer"
            value={settings.activeProfileId}
            onChange={(event) => onProfileSelect(event.target.value)}
          >
            {settings.profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
        <select
          aria-label="Call type"
          title="What kind of call this is — shapes how the answer is written (saved to the active profile)"
          value={settings?.callType ?? "behavioral"}
          disabled={!settings}
          onChange={(event) => onCallTypeSelect(event.target.value as CallType)}
        >
          {(settings?.callTypes ?? []).map((choice) => (
            <option key={choice.id} value={choice.id}>
              {choice.label}
            </option>
          ))}
        </select>
      </div>

      <div className="style-chips" role="group" aria-label="Answer style">
        {ANSWER_STYLES.map((style) => (
          <button
            key={style.id}
            type="button"
            className="style-chip"
            aria-pressed={settings?.answerStyle === style.id}
            title="Answer length — applies to the next answer; press Regenerate to re-answer"
            onClick={() => onStyleSelect(style.id)}
          >
            {style.label}
          </button>
        ))}
      </div>

      {state.error && (
        <div className="error-box" role="alert">
          <span>{state.error.message}</span>
          <button type="button" className="mini-button" onClick={dismissError}>
            Dismiss
          </button>
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
