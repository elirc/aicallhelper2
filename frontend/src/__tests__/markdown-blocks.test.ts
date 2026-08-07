import { describe, expect, it } from "vitest";

import { parseBlocks } from "../markdown/blocks";

describe("headings", () => {
  it("demotes model headings: # -> h3, capped at h6", () => {
    expect(parseBlocks("# One")[0]).toEqual({ type: "heading", level: 3, text: "One" });
    expect(parseBlocks("## Two")[0]).toEqual({ type: "heading", level: 4, text: "Two" });
    expect(parseBlocks("#### Four")[0]).toEqual({
      type: "heading",
      level: 6,
      text: "Four",
    });
    expect(parseBlocks("###### Six")[0]).toEqual({
      type: "heading",
      level: 6,
      text: "Six",
    });
  });

  it("strips trailing closing hashes", () => {
    expect(parseBlocks("# Title ##")[0]).toEqual({
      type: "heading",
      level: 3,
      text: "Title",
    });
  });
});

describe("paragraphs", () => {
  it("joins consecutive lines, splits on blank lines", () => {
    const blocks = parseBlocks("line one\nline two\n\nsecond para");
    expect(blocks).toEqual([
      { type: "paragraph", text: "line one\nline two" },
      { type: "paragraph", text: "second para" },
    ]);
  });
});

describe("fenced code", () => {
  it("drops the info string and preserves content verbatim", () => {
    const blocks = parseBlocks("```python\nx = 1\n\n<b>not html</b>\n```");
    expect(blocks).toEqual([{ type: "code", text: "x = 1\n\n<b>not html</b>" }]);
  });

  it("supports tildes and longer closing fences", () => {
    expect(parseBlocks("~~~\ncode\n~~~~")).toEqual([{ type: "code", text: "code" }]);
  });

  it("a shorter or wrong-char fence does not close", () => {
    expect(parseBlocks("````\ncode\n```\nmore\n````")).toEqual([
      { type: "code", text: "code\n```\nmore" },
    ]);
  });

  it("unterminated fence at EOF is an open block (streaming)", () => {
    expect(parseBlocks("```\npartial cod")).toEqual([
      { type: "code", text: "partial cod" },
    ]);
  });
});

describe("thematic breaks", () => {
  it("recognizes ---, ***, ___ with spacing", () => {
    expect(parseBlocks("---")[0]).toEqual({ type: "hr" });
    expect(parseBlocks("* * *")[0]).toEqual({ type: "hr" });
    expect(parseBlocks("___")[0]).toEqual({ type: "hr" });
  });

  it("hr wins over a - - - list reading", () => {
    expect(parseBlocks("- - -")).toEqual([{ type: "hr" }]);
  });
});

describe("lists", () => {
  it("bullet list with -, *, +", () => {
    const blocks = parseBlocks("- a\n- b");
    expect(blocks).toEqual([
      {
        type: "list",
        ordered: false,
        start: 1,
        loose: false,
        items: [{ text: "a" }, { text: "b" }],
      },
    ]);
  });

  it("ordered list keeps its start number, n. and n) forms", () => {
    const dot = parseBlocks("3. c\n4. d");
    expect(dot[0]).toMatchObject({ type: "list", ordered: true, start: 3 });
    const paren = parseBlocks("1) x\n2) y");
    expect(paren[0]).toMatchObject({ type: "list", ordered: true, start: 1 });
  });

  it("lazy continuation of wrapped items", () => {
    const blocks = parseBlocks("- first item\n  wrapped line\n- second");
    expect(blocks[0]).toMatchObject({
      items: [{ text: "first item\nwrapped line" }, { text: "second" }],
    });
  });

  it("loose list: a blank line ends the list only if no item follows", () => {
    const loose = parseBlocks("- a\n\n- b");
    expect(loose[0]).toMatchObject({ type: "list", loose: true });
    const ended = parseBlocks("- a\n\nparagraph");
    expect(ended).toEqual([
      { type: "list", ordered: false, start: 1, loose: false, items: [{ text: "a" }] },
      { type: "paragraph", text: "paragraph" },
    ]);
  });
});
