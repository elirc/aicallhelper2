/**
 * Block-level markdown parsing for the answer panel — a deliberate SUBSET.
 *
 * Model output is UNTRUSTED. This parser produces plain data (never HTML
 * strings); the renderer emits every string as a DOM text node. Links are
 * deliberately NOT parsed — `[text](url)` stays literal text, so there is
 * no href to sanitize and no javascript: to smuggle.
 *
 * The parser is a pure function of the full source. Streaming safety falls
 * out of that: rendering any prefix cannot throw, and re-rendering the full
 * text always produces the same blocks (the streaming-vs-batch invariant is
 * tested over every cut point of a corpus).
 */

export interface ListItem {
  /** Raw item text; lines joined with \n (lazy continuation of wrapped items). */
  text: string;
}

export type Block =
  | { type: "paragraph"; text: string }
  | { type: "heading"; level: 3 | 4 | 5 | 6; text: string }
  | { type: "code"; text: string }
  | { type: "hr" }
  | {
      type: "list";
      ordered: boolean;
      start: number;
      loose: boolean;
      items: ListItem[];
    };

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})\s*(.*)$/;
const HR_RE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const HEADING_RE = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$/;
const HEADING_EMPTY_RE = /^ {0,3}(#{1,6})[ \t]*$/;
const BULLET_RE = /^ {0,3}([-*+])[ \t]+(.*)$/;
const ORDERED_RE = /^ {0,3}(\d{1,9})([.)])[ \t]+(.*)$/;
const INDENT_CONT_RE = /^ {2,}\S/;

function isBlank(line: string): boolean {
  return line.trim() === "";
}

function bulletStart(line: string): RegExpMatchArray | null {
  // hr wins over a "- - -" style list item.
  if (HR_RE.test(line)) return null;
  return line.match(BULLET_RE);
}

function orderedStart(line: string): RegExpMatchArray | null {
  return line.match(ORDERED_RE);
}

function startsOtherBlock(line: string): boolean {
  return (
    FENCE_RE.test(line) ||
    HR_RE.test(line) ||
    HEADING_RE.test(line) ||
    HEADING_EMPTY_RE.test(line) ||
    bulletStart(line) !== null ||
    orderedStart(line) !== null
  );
}

export function parseBlocks(source: string): Block[] {
  // Normalize line endings first. Every block regex below is `$`-anchored and
  // neither `.` nor `[ \t]` matches `\r`, so a CRLF document would degrade
  // EVERY construct — headings, rules, both list kinds, opening fences — into
  // paragraphs, silently rendering something other than what the model wrote.
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i] ?? "";

    if (isBlank(line)) {
      i += 1;
      continue;
    }

    const fence = line.match(FENCE_RE);
    if (fence) {
      const marker = fence[1] ?? "```";
      const char = marker.charAt(0);
      const minLen = marker.length;
      const content: string[] = [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        const candidate = lines[i] ?? "";
        const close = candidate.match(FENCE_RE);
        if (
          close &&
          (close[1] ?? "").charAt(0) === char &&
          (close[1] ?? "").length >= minLen &&
          (close[2] ?? "") === ""
        ) {
          closed = true;
          i += 1;
          break;
        }
        content.push(candidate);
        i += 1;
      }
      void closed; // unterminated fence at EOF = open block, same rendering
      blocks.push({ type: "code", text: content.join("\n") });
      continue;
    }

    if (HR_RE.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    const heading = line.match(HEADING_RE);
    if (heading) {
      const hashes = (heading[1] ?? "#").length;
      // Demoted: model # -> h3, capped at h6 — the page owns h1/h2.
      const level = Math.min(hashes + 2, 6) as 3 | 4 | 5 | 6;
      blocks.push({ type: "heading", level, text: heading[2] ?? "" });
      i += 1;
      continue;
    }
    const emptyHeading = line.match(HEADING_EMPTY_RE);
    if (emptyHeading) {
      const level = Math.min((emptyHeading[1] ?? "#").length + 2, 6) as 3 | 4 | 5 | 6;
      blocks.push({ type: "heading", level, text: "" });
      i += 1;
      continue;
    }

    const bullet = bulletStart(line);
    const ordered = bullet ? null : orderedStart(line);
    if (bullet || ordered) {
      const isOrdered = ordered !== null;
      const start = isOrdered ? parseInt(ordered[1] ?? "1", 10) : 1;
      const items: ListItem[] = [];
      let loose = false;
      let current: string[] = bullet
        ? [bullet[2] ?? ""]
        : [ordered?.[3] ?? ""];
      i += 1;
      while (i < lines.length) {
        const next = lines[i] ?? "";
        const nextBullet = isOrdered ? null : bulletStart(next);
        const nextOrdered = isOrdered ? orderedStart(next) : null;
        if (nextBullet || nextOrdered) {
          items.push({ text: current.join("\n") });
          current = [nextBullet ? nextBullet[2] ?? "" : nextOrdered?.[3] ?? ""];
          i += 1;
          continue;
        }
        if (isBlank(next)) {
          // A blank line ends the list ONLY if no item follows (loose list).
          let j = i + 1;
          while (j < lines.length && isBlank(lines[j] ?? "")) j += 1;
          const after = lines[j] ?? "";
          const afterIsItem = isOrdered
            ? orderedStart(after) !== null
            : bulletStart(after) !== null;
          if (j < lines.length && afterIsItem) {
            loose = true;
            i = j;
            continue;
          }
          break;
        }
        if (INDENT_CONT_RE.test(next) || !startsOtherBlock(next)) {
          // Lazy continuation of a wrapped item.
          current.push(next.trim());
          i += 1;
          continue;
        }
        break;
      }
      items.push({ text: current.join("\n") });
      blocks.push({ type: "list", ordered: isOrdered, start, loose, items });
      continue;
    }

    // Paragraph: collect until a blank line or another block start.
    const para: string[] = [line.trim()];
    i += 1;
    while (i < lines.length) {
      const next = lines[i] ?? "";
      if (isBlank(next) || startsOtherBlock(next)) break;
      para.push(next.trim());
      i += 1;
    }
    blocks.push({ type: "paragraph", text: para.join("\n") });
  }

  return blocks;
}

/** Stable signature per block, used for memoized per-block rendering. */
export function blockSignature(block: Block): string {
  switch (block.type) {
    case "paragraph":
      return `p:${block.text}`;
    case "heading":
      return `h${block.level}:${block.text}`;
    case "code":
      return `c:${block.text}`;
    case "hr":
      return "hr";
    case "list":
      // JSON.stringify, not a joined separator: item text is untrusted model
      // output, so ANY separator character can appear inside it (U+001F is an
      // ordinary character that trim() and \s both leave alone). A collision
      // here is not cosmetic — BlockView memoizes on this signature, so two
      // different lists sharing one signature leave stale DOM on screen.
      return `l:${block.ordered ? block.start : "-"}:${block.loose ? 1 : 0}:${JSON.stringify(
        block.items.map((item) => item.text),
      )}`;
  }
}
