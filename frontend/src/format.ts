import type { Metrics } from "./types";

/** 75 -> "1:15" */
export function formatMmSs(totalSeconds: number): string {
  const clamped = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(clamped / 60);
  const seconds = clamped % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

/** 1234 -> "1.2s to first word" */
export function formatLatencyChip(metrics: Metrics): string {
  return `${(metrics.firstTokenMs / 1000).toFixed(1)}s to first word`;
}

export function formatLatencyTitle(metrics: Metrics): string {
  return (
    `First word ${metrics.firstTokenMs} ms after Stop · ` +
    (typeof metrics.audioDrainMs === "number"
      ? `audio drained ${metrics.audioDrainMs} ms · `
      : "") +
    `transcript finalized ${metrics.sttFinalizeMs} ms · ` +
    `full answer ${(metrics.totalMs / 1000).toFixed(1)} s`
  );
}

/** "ctrl+shift+space" -> "Ctrl+Shift+Space" */
export function formatHotkey(accelerator: string): string {
  return accelerator
    .split("+")
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
    .map((part) =>
      part.length <= 1 ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1),
    )
    .join("+");
}
