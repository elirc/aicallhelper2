import { useEffect, useRef, useState } from "react";

import { copyToClipboard } from "../clipboard";
import { formatLatencyChip, formatLatencyTitle } from "../format";
import { Markdown } from "../markdown/Markdown";
import { finishNotice, type Entry } from "../state";
import { ANSWER_FONT_MAX, ANSWER_FONT_MIN } from "../types";

const STICKY_BOTTOM_PX = 28;

interface Props {
  entry: Entry | null;
  answering: boolean;
  canRegenerate: boolean;
  onRegenerate: () => void;
  onCopyError: (message: string) => void;
  onAnnounce: (text: string) => void;
  /** Identity of the viewed history entry — a change resets scroll to top. */
  viewKey: string;
  /** Answer text size in px (persisted); null until settings arrive. */
  fontPx: number | null;
  onFontStep: (delta: number) => void;
  /** Label of the call type this answer was generated under, if known. */
  callTypeLabel: string | null;
}

export function AnswerPanel({
  entry,
  answering,
  canRegenerate,
  onRegenerate,
  onCopyError,
  onAnnounce,
  viewKey,
  fontPx,
  onFontStep,
  callTypeLabel,
}: Props) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const wasAtBottomRef = useRef(true);
  const [copied, setCopied] = useState(false);
  const answer = entry?.answer ?? "";
  const notice = entry && !entry.live ? finishNotice(entry.finish) : null;

  // Track whether the user is at the bottom BEFORE the content grows.
  const handleScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    wasAtBottomRef.current =
      el.scrollTop + el.clientHeight >= el.scrollHeight - STICKY_BOTTOM_PX;
  };

  useEffect(() => {
    // Stick to the bottom only if already at the bottom — a user who
    // scrolled up to re-read must not be yanked down by each token.
    const el = bodyRef.current;
    if (el && wasAtBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [answer]);

  useEffect(() => {
    // Switching history entries resets scroll to top.
    const el = bodyRef.current;
    if (el) el.scrollTop = 0;
    wasAtBottomRef.current = true;
  }, [viewKey]);

  const copy = async () => {
    try {
      // The markdown SOURCE — bullets survive pasting.
      await copyToClipboard(answer);
      setCopied(true);
      onAnnounce("Answer copied to clipboard");
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      onCopyError("Could not copy to the clipboard.");
    }
  };

  return (
    <section className="panel panel-answer" aria-label="Suggested answer">
      <div className="panel-title">
        <h2>Suggested answer</h2>
        {answering && <span className="tag tag-generating">generating…</span>}
        {entry?.metrics && (
          <span className="latency-chip" title={formatLatencyTitle(entry.metrics)}>
            {formatLatencyChip(entry.metrics)}
          </span>
        )}
        {callTypeLabel && !entry?.live && (
          <span className="tag" title="Call type this answer was written for">
            {callTypeLabel}
          </span>
        )}
        <span className="panel-actions">
          {fontPx !== null && (
            <>
              <button
                type="button"
                className="mini-button"
                aria-label="Smaller text"
                title="Smaller text"
                onClick={() => onFontStep(-1)}
                disabled={fontPx <= ANSWER_FONT_MIN}
              >
                A−
              </button>
              <button
                type="button"
                className="mini-button"
                aria-label="Larger text"
                title="Larger text"
                onClick={() => onFontStep(1)}
                disabled={fontPx >= ANSWER_FONT_MAX}
              >
                A+
              </button>
            </>
          )}
          {canRegenerate && (
            <button type="button" className="mini-button" onClick={onRegenerate}>
              Regenerate
            </button>
          )}
          {answer.trim() !== "" && (
            <button type="button" className="mini-button" onClick={() => void copy()}>
              {copied ? "Copied ✓" : "Copy"}
            </button>
          )}
        </span>
      </div>
      <div
        ref={bodyRef}
        className="panel-body answer-body"
        aria-live="polite"
        aria-busy={answering}
        onScroll={handleScroll}
        style={fontPx !== null ? { fontSize: `${fontPx}px` } : undefined}
      >
        {answer ? (
          <Markdown source={answer} />
        ) : (
          <span className="placeholder">Your AI-suggested answer will stream here.</span>
        )}
      </div>
      {notice && (
        <p className="answer-finish-notice" role="note">
          {notice}
        </p>
      )}
    </section>
  );
}
