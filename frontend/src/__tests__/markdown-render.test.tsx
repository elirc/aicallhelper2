import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "../markdown/Markdown";

function html(source: string): string {
  const { container, unmount } = render(<Markdown source={source} />);
  const result = container.innerHTML;
  unmount();
  return result;
}

describe("inline rendering", () => {
  it("bold and italic", () => {
    expect(html("**bold** and *ital*")).toBe(
      "<p><strong>bold</strong> and <em>ital</em></p>",
    );
  });

  it("nested emphasis", () => {
    expect(html("**bold *both* bold**")).toBe(
      "<p><strong>bold <em>both</em> bold</strong></p>",
    );
  });

  it("underscore emphasis works between words", () => {
    expect(html("say _hello_ now")).toBe("<p>say <em>hello</em> now</p>");
  });

  it("snake_case must not italicize", () => {
    expect(html("use snake_case_names here")).toBe("<p>use snake_case_names here</p>");
    expect(html("a_b_c")).toBe("<p>a_b_c</p>");
  });

  it("intraword asterisk emphasis is allowed (CommonMark)", () => {
    expect(html("a*b*c")).toBe("<p>a<em>b</em>c</p>");
  });

  it("unmatched delimiters stay literal", () => {
    expect(html("2 * 3 equals 6")).toBe("<p>2 * 3 equals 6</p>");
    expect(html("*open only")).toBe("<p>*open only</p>");
  });

  it("inline code with exact-length closers", () => {
    expect(html("run `npm test` now")).toBe("<p>run <code>npm test</code> now</p>");
    expect(html("``code with ` inside``")).toBe(
      "<p><code>code with ` inside</code></p>",
    );
  });

  it("one space of padding stripped from code spans", () => {
    expect(html("` code `")).toBe("<p><code>code</code></p>");
    expect(html("`  two  `")).toBe("<p><code> two </code></p>");
  });

  it("emphasis markers inside code are literal", () => {
    expect(html("`*not em*`")).toBe("<p><code>*not em*</code></p>");
  });

  it("backslash escapes", () => {
    expect(html("\\*not em\\*")).toBe("<p>*not em*</p>");
    expect(html("\\`not code\\`")).toBe("<p>`not code`</p>");
  });

  it("links are deliberately NOT parsed — no href exists at all", () => {
    const out = html("[click me](https://evil.example)");
    expect(out).toBe("<p>[click me](https://evil.example)</p>");
    expect(out).not.toContain("<a");
  });

  it("javascript: link syntax stays literal text", () => {
    const { container, unmount } = render(
      <Markdown source={"[x](javascript:alert(1))"} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toBe("[x](javascript:alert(1))");
    unmount();
  });
});

describe("XSS suite — model output is untrusted", () => {
  const payloads = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "```\n</pre><script>alert(1)</script>\n```",
    '" onmouseover="alert(1)" data-x="',
    "<svg/onload=alert(1)>",
    "<iframe src='javascript:alert(1)'></iframe>",
    "**<b>bold</b>**",
    "&lt;already&gt; &amp; entities",
  ];

  it.each(payloads)("renders %s as literal text with zero live markup", (payload) => {
    const { container, unmount } = render(<Markdown source={payload} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
    // No element anywhere carries any attribute derived from model text
    // (start is legitimate on <ol> and React adds nothing else here).
    for (const el of Array.from(container.querySelectorAll("*"))) {
      for (const attr of Array.from(el.attributes)) {
        expect(["start"]).toContain(attr.name);
      }
    }
    unmount();
  });

  it("keeps the raw payload visible as text", () => {
    const { container, unmount } = render(
      <Markdown source={"<script>alert(1)</script>"} />,
    );
    expect(container.textContent).toContain("<script>alert(1)</script>");
    unmount();
  });
});

describe("streaming invariant", () => {
  const corpus = [
    "# Answer\n\nFirst **bold** point.\n\n- one\n- two wrapped\n  line\n\n1. a\n2. b",
    "```python\nprint('hi')\n```\ntail *em* text",
    "Para one\n\n---\n\n_Para_ two `code span` end",
    "- loose\n\n- list\n\nafter **the** list",
    "Answer with unterminated ```\ncode at eof",
    "*open em never closes\n\n## head",
  ];

  it("every cut point renders identically to a batch render", () => {
    // One reused root: prefix -> full -> compare, for EVERY cut point.
    for (const doc of corpus) {
      const batch = html(doc);
      const { container, rerender, unmount } = render(<Markdown source="" />);
      for (let cut = 0; cut <= doc.length; cut += 1) {
        rerender(<Markdown source={doc.slice(0, cut)} />);
        rerender(<Markdown source={doc} />);
        expect(container.innerHTML, `cut at ${cut}`).toBe(batch);
      }
      unmount();
    }
  });

  it("rendering any prefix never throws", () => {
    for (const doc of corpus) {
      const { rerender, unmount } = render(<Markdown source="" />);
      for (let cut = 0; cut <= doc.length; cut += 1) {
        rerender(<Markdown source={doc.slice(0, cut)} />);
      }
      unmount();
    }
  });

  it("completed blocks keep their DOM nodes across streaming updates", () => {
    const { container, rerender } = render(
      <Markdown source={"First paragraph.\n\nSecond gro"} />,
    );
    const firstBefore = container.querySelector("p");
    rerender(<Markdown source={"First paragraph.\n\nSecond growing longer."} />);
    const firstAfter = container.querySelector("p");
    expect(firstAfter).toBe(firstBefore); // identity, not equality
  });

  it("an unchanged source is a no-op", () => {
    const source = "Stable **text**";
    const { container, rerender } = render(<Markdown source={source} />);
    const strongBefore = container.querySelector("strong");
    rerender(<Markdown source={source} />);
    expect(container.querySelector("strong")).toBe(strongBefore);
  });
});
