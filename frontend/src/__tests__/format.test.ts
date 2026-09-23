import { describe, expect, it } from "vitest";

import { formatHotkey, formatLatencyChip, formatLatencyTitle, formatMmSs } from "../format";

describe("formatMmSs", () => {
  it("formats zero, seconds, and minutes", () => {
    expect(formatMmSs(0)).toBe("0:00");
    expect(formatMmSs(9)).toBe("0:09");
    expect(formatMmSs(75)).toBe("1:15");
    expect(formatMmSs(120)).toBe("2:00");
  });

  it("clamps negatives", () => {
    expect(formatMmSs(-5)).toBe("0:00");
  });
});

describe("latency formatting", () => {
  const metrics = { sttFinalizeMs: 210, firstTokenMs: 940, totalMs: 2130 };

  it("chip shows first-word seconds to one decimal", () => {
    expect(formatLatencyChip(metrics)).toBe("0.9s to first word");
  });

  it("title breaks down all three numbers", () => {
    expect(formatLatencyTitle(metrics)).toBe(
      "First word 940 ms after Stop · transcript finalized 210 ms · full answer 2.1 s",
    );
  });

  it("title includes the audio drain stage when the core reports it", () => {
    expect(formatLatencyTitle({ ...metrics, audioDrainMs: 35 })).toBe(
      "First word 940 ms after Stop · audio drained 35 ms · transcript finalized 210 ms · full answer 2.1 s",
    );
  });
});

describe("formatHotkey", () => {
  it("capitalizes each part", () => {
    expect(formatHotkey("ctrl+shift+space")).toBe("Ctrl+Shift+Space");
    expect(formatHotkey("Ctrl+Shift+Space")).toBe("Ctrl+Shift+Space");
  });

  it("single characters upper-case", () => {
    expect(formatHotkey("alt+r")).toBe("Alt+R");
  });

  it("tolerates stray whitespace", () => {
    expect(formatHotkey(" ctrl + f5 ")).toBe("Ctrl+F5");
  });
});
