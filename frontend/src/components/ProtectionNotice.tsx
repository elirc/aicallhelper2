import type { ProtectionVerdict } from "../types";

interface Props {
  verdict: ProtectionVerdict;
  /** The prompter strip has room for one short line only. */
  compact?: boolean;
}

/**
 * The capture-protection verdict, rendered in EVERY view (full, prompter,
 * Settings). A failure is an alert — believing you are hidden while being
 * broadcast is this app's worst failure. Before Windows has answered the UI
 * says so rather than implying protection it has not confirmed. Even a
 * "protected" verdict is what Windows reports, not a guarantee for every
 * capture method, so the wording stays modest.
 */
export function ProtectionNotice({ verdict, compact = false }: Props) {
  if (verdict === "unprotected") {
    return (
      <p
        className={compact ? "prompter-notice" : "protection-warning"}
        role="alert"
        data-protection="unprotected"
      >
        {compact
          ? "Not hidden from screen share — Windows refused to protect this window."
          : "Windows would not hide this window from screen capture, so it may be visible if you share your screen."}
      </p>
    );
  }
  if (verdict === "unknown") {
    return (
      <span
        className="protection-chip protection-chip-unknown"
        data-protection="unknown"
        title="Windows has not yet confirmed that this window is excluded from screen capture."
      >
        Screen-share protection not confirmed yet
      </span>
    );
  }
  return (
    <span
      className="protection-chip"
      data-protection="protected"
      title="Windows reports this window as excluded from screen capture. Check your sharing app once before an important call."
    >
      Hidden from screen capture
    </span>
  );
}
