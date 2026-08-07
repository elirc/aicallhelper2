import { useEffect, useRef, useState } from "react";

import type { Entry } from "../App";
import { copyToClipboard } from "../clipboard";
import { formatLatencyChip, formatLatencyTitle } from "../format";
import { Markdown } from "../markdown/Markdown";

const STICKY_BOTTOM_PX = 28;

interface Props {
  entry: Entry | null;
  answering: boolean;
  canRegenerate: boolean;
  onRegenerate: () => void;
  onCopyError: (message: string) => void;
  onAnnounce: (text: string) => void;
  /** Changes when the viewed history entry changes — resets scroll to top. */
  viewKey: number;
}

export function AnswerPanel({
  entry,
  answering,
  canRegenerate,
  onRegenerate,
  onCopyError,
  onAnnounce,
  viewKey,
}: Props) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const wasAtBottomRef = useRef(true);
  const [copied, setCopied] = useState(false);
  const answer = entry?.answer ?? "";

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
        <span className="panel-actions">
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
      >
        {answer ? (
          <Markdown source={answer} />
        ) : (
          <span className="placeholder">Your AI-suggested answer will stream here.</span>
        )}
      </div>
    </section>
  );
}
