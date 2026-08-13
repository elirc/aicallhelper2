import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "../markdown/Markdown";
import { blockSignature, parseBlocks } from "../markdown/blocks";

function html(source: string): string {
  const { container, unmount } = render(<Markdown source={source} />);
  const result = container.innerHTML;
  unmount();
  return result;
}

describe("hostile-input hardening", () => {
  it("deeply nested emphasis renders instead of blanking the app", () => {
    // Unbounded nesting used to overflow the stack inside render, and React
    // unmounts the WHOLE root on an uncaught render error — one pathological
    // answer could blank the app mid-call.
    for (const k of [1000, 5000, 12000]) {
      const source = "*".repeat(k) + "x" + "*".repeat(k);
      const { container, unmount } = render(<Markdown source={source} />);
      expect(container.innerHTML.length).toBeGreaterThan(0);
      expect(container.textContent).toContain("x");
      unmount();
    }
  });

  it("delimiter-dense text parses fast enough not to freeze the UI", () => {
    // Emphasis resolution was quadratic, and the renderer re-parses the whole
    // source on every streamed chunk — 18 KB of "*a*" froze the main thread
    // for a minute.
    const source = "*a*".repeat(8000);
    const started = performance.now();
    const { unmount } = render(<Markdown source={source} />);
    const elapsed = performance.now() - started;
    unmount();
    expect(elapsed).toBeLessThan(3000);
  });

  it("unmatched-delimiter text is also fast (no quadratic opener rescan)", () => {
    const source = "a*".repeat(16000);
    const started = performance.now();
    const { unmount } = render(<Markdown source={source} />);
    const elapsed = performance.now() - started;
    unmount();
    expect(elapsed).toBeLessThan(3000);
  });

  it("very long input still renders as text past the emphasis guard", () => {
    const source = "word ".repeat(6000); // > 20k chars
    const { container, unmount } = render(<Markdown source={source} />);
    expect(container.textContent).toContain("word");
    unmount();
  });

  it("normal emphasis still works right below the depth cap", () => {
    const { container, unmount } = render(<Markdown source={"**bold _it_ bold**"} />);
    expect(container.querySelector("strong")).not.toBeNull();
    expect(container.querySelector("em")).not.toBeNull();
    unmount();
  });
});

describe("CRLF normalization", () => {
  it("CRLF documents render identically to LF documents", () => {
    // Every block regex is $-anchored and cannot match a trailing \r, so a
    // CRLF answer used to degrade EVERY construct into a paragraph.
    const lf = "# Title\n\n- one\n- two\n\n```js\ncode\n```\n\n---\n";
    const rendered = html(lf);
    expect(html(lf.replace(/\n/g, "\r\n"))).toBe(rendered);
    expect(rendered).toContain("<h3>");
    expect(rendered).toContain("<li>");
    expect(rendered).toContain("<pre>");
    expect(rendered).toContain("<hr>");
  });

  it("lone CR is normalized too", () => {
    expect(html("# Title\r\rbody")).toBe(html("# Title\n\nbody"));
  });

  it("CRLF inside a fenced block does not leak carriage returns into the DOM", () => {
    const { container, unmount } = render(<Markdown source={"```\r\nline\r\n```\r\n"} />);
    expect(container.querySelector("code")?.textContent).toBe("line");
    unmount();
  });
});

describe("block signature identity", () => {
  it("lists whose item text concatenates identically get different signatures", () => {
    // The old signature joined items with a separator that can itself appear
    // in model output, so "- ab" and "- a\n- b" collided.
    const oneItem = parseBlocks("- ab")[0];
    const twoItems = parseBlocks("- a\n- b")[0];
    expect(oneItem && twoItems).toBeTruthy();
    expect(blockSignature(oneItem!)).not.toBe(blockSignature(twoItems!));
  });

  it("a collision would leave stale DOM — it does not", () => {
    const { container, rerender, unmount } = render(<Markdown source={"- ab"} />);
    expect(container.querySelectorAll("li")).toHaveLength(1);
    rerender(<Markdown source={"- a\n- b"} />);
    expect(container.querySelectorAll("li")).toHaveLength(2);
    rerender(<Markdown source={"1. xy"} />);
    expect(container.querySelectorAll("li")).toHaveLength(1);
    rerender(<Markdown source={"1. x\n2. y"} />);
    expect(container.querySelectorAll("li")).toHaveLength(2);
    unmount();
  });

  it("separator-like control characters inside item text cannot collide", () => {
    const sep = String.fromCharCode(0x1f);
    const a = parseBlocks(`- a${sep}b`)[0];
    const b = parseBlocks("- a\n- b")[0];
    expect(blockSignature(a!)).not.toBe(blockSignature(b!));
  });
});

describe("emphasis resolution soundness", () => {
  // The opener-floor optimization must never change what a document means.
  // A failed `_` closer records a floor; a later `*` pair splices below it and
  // shifts every index — a stale floor then hid real emphasis.
  it("a * pair does not swallow later _ emphasis", () => {
    const out = html("*the foo_ and bar_ conventions* use _trailing_ underscores");
    expect(out).toContain("<em>trailing</em>");
  });

  it.each([
    ["*a_ b_ c* then _d_", "<em>d</em>"],
    ["**x_ y_ z** and _q_", "<em>q</em>"],
    ["_a* b* c_ then *d*", "<em>d</em>"],
  ])("keeps emphasis in %s", (source, expected) => {
    expect(html(source)).toContain(expected);
  });

  it("still resolves ordinary mixed emphasis", () => {
    expect(html("**bold** and *ital* and _under_")).toBe(
      "<p><strong>bold</strong> and <em>ital</em> and <em>under</em></p>",
    );
  });
});
