import { useEffect, useRef, useState } from "react";

import { formatMmSs } from "../format";
import { Markdown } from "../markdown/Markdown";
import { ProtectionNotice } from "./ProtectionNotice";
import { finishNotice, type Entry, type Phase } from "../state";
import {
  ANSWER_STYLES,
  PROMPTER_FONT_MAX,
  PROMPTER_FONT_MIN,
  type AnswerStyle,
  type AppErrorPayload,
  type ProtectionVerdict,
} from "../types";

interface Props {
  entry: Entry | null;
  phase: Phase;
  seconds: number;
  recordLabel: string;
  answering: boolean;
  error: AppErrorPayload | null;
  protection: ProtectionVerdict;
  /** Status for the non-obvious phases (starting/finalizing/answering); the
   *  strip has no status line, so without it Finalizing looked like Record. */
  status: string;
  silent: boolean;
  answerStyle: AnswerStyle | null;
  fontPx: number;
  historyCount: number;
  historyIndex: number;
  onRecordToggle: () => void;
  onStyleSelect: (style: AnswerStyle) => void;
  onPrev: () => void;
  onNext: () => void;
  onFontStep: (delta: number) => void;
  onDock: () => void;
  onExit: () => void;
  onDismissError: () => void;
  /** Terminal core-failure text (core:failed), or null. */
  coreFailed?: string | null;
}

/**
 * The teleprompter strip: one wide, short, always-on-top window docked under
 * the webcam, showing the answer in large text and nothing else that is not
 * needed mid-call. The answer is TOP-ANCHORED while streaming — a balanced
 * answer streams in faster than it can be read, and sticky-bottom scrolling
 * would drag the opening off-screen while the user is still saying it.
 */
export function PrompterView({
  entry,
  phase,
  seconds,
  recordLabel,
  answering,
  error,
  protection,
  status,
  silent,
  answerStyle,
  fontPx,
  historyCount,
  historyIndex,
  onRecordToggle,
  onStyleSelect,
  onPrev,
  onNext,
  onFontStep,
  onDock,
  onExit,
  onDismissError,
  coreFailed = null,
}: Props) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const answer = entry?.answer ?? "";
  const question = entry?.question ?? "";
  const viewKey = entry?.id ?? "";

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onExit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onExit]);

  useEffect(() => {
    // A new entry starts at the top; streaming never moves the viewport.
    const el = bodyRef.current;
    if (el) el.scrollTop = 0;
  }, [viewKey]);

  const measure = () => {
    const el = bodyRef.current;
    if (!el) return;
    setOverflowing(el.scrollHeight - el.scrollTop - el.clientHeight > 4);
  };

  useEffect(measure, [answer, fontPx]);

  const questionText =
    question !== "" ? question : phase === "recording" ? "Listening…" : "";

  return (
    <div className="prompter">
      <div className="prompter-bar">
        <span
          className={`status-dot ${phase !== "idle" ? "status-dot-active" : ""}`}
          aria-hidden="true"
        />
        <button
          type="button"
          className={`record-button record-${phase}`}
          onClick={onRecordToggle}
          // aria-disabled, not disabled: a keyboard user who just pressed
          // Stop keeps focus here; the click is ignored while finalizing.
          aria-disabled={phase === "finalizing"}
        >
          {recordLabel}
        </button>
        {phase === "recording" && <span className="timer">{formatMmSs(seconds)}</span>}
        {phase !== "idle" && phase !== "recording" && (
          <span className="prompter-status" role="status">
            {status}
          </span>
        )}
        <span className="prompter-question" title={question}>
          {questionText}
        </span>
        <span className="style-chips" role="group" aria-label="Answer style">
          {ANSWER_STYLES.map((style) => (
            <button
              key={style.id}
              type="button"
              className="style-chip"
              aria-pressed={answerStyle === style.id}
              onClick={() => onStyleSelect(style.id)}
            >
              {style.label}
            </button>
          ))}
        </span>
        {historyCount > 1 && (
          <>
            <button
              type="button"
              className="mini-button"
              aria-label="Previous answer"
              onClick={onPrev}
              disabled={historyIndex === 0}
            >
              ←
            </button>
            <span className="history-label">
              {historyIndex + 1}/{historyCount}
            </span>
            <button
              type="button"
              className="mini-button"
              aria-label="Next answer"
              onClick={onNext}
              disabled={historyIndex >= historyCount - 1}
            >
              →
            </button>
          </>
        )}
        {protection !== "unprotected" && <ProtectionNotice verdict={protection} compact />}
        <button
          type="button"
          className="mini-button"
          aria-label="Smaller text"
          title="Smaller text"
          onClick={() => onFontStep(-1)}
          disabled={fontPx <= PROMPTER_FONT_MIN}
        >
          A−
        </button>
        <button
          type="button"
          className="mini-button"
          aria-label="Larger text"
          title="Larger text"
          onClick={() => onFontStep(1)}
          disabled={fontPx >= PROMPTER_FONT_MAX}
        >
          A+
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label="Dock under camera"
          title="Dock under camera (top-centre of the screen)"
          onClick={onDock}
        >
          ⤒
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label="Exit prompter"
          title="Back to the full window (Esc)"
          onClick={onExit}
        >
          ⤢
        </button>
      </div>
      {protection === "unprotected" && <ProtectionNotice verdict={protection} compact />}
      {coreFailed && (
        <p className="prompter-notice prompter-notice-error" role="alert">
          {coreFailed}
        </p>
      )}
      {error && (
        <p className="prompter-notice prompter-notice-error" role="alert">
          {error.message}{" "}
          <button type="button" className="link-button" onClick={onDismissError}>
            Dismiss
          </button>
        </p>
      )}
      {entry && !entry.live && finishNotice(entry.finish) && (
        <p className="prompter-notice" role="note">
          {finishNotice(entry.finish)}
        </p>
      )}
      {silent && (
        <p className="prompter-notice" role="status">
          No call audio detected — check that the call plays through your speakers.
        </p>
      )}
      <div
        ref={bodyRef}
        className="answer-body prompter-body"
        aria-label="Suggested answer"
        aria-live="polite"
        aria-busy={answering}
        onScroll={measure}
        style={{ fontSize: `${fontPx}px` }}
      >
        <div className="prompter-column">
          {answer ? (
            <Markdown source={answer} />
          ) : (
            <span className="placeholder">
              {phase === "recording"
                ? "Listening — press Stop & Answer when they finish."
                : "Your answer will appear here, under the camera."}
            </span>
          )}
        </div>
        {overflowing && (
          <span className="prompter-more" aria-hidden="true">
            ▼ more
          </span>
        )}
      </div>
    </div>
  );
}
