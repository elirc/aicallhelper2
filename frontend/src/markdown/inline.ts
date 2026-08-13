/**
 * Inline markdown parsing: bold, italic (CommonMark-ish flanking rules —
 * snake_case must not italicize), inline code (backtick runs with
 * exact-length closers, one space of padding stripped), backslash escapes.
 * Links are NOT parsed. Output is plain data; every string becomes a DOM
 * text node in the renderer.
 */

export type InlineNode =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; children: InlineNode[] }
  | { kind: "em"; children: InlineNode[] };

type Token =
  | { t: "text"; s: string }
  | { t: "code"; s: string }
  | { t: "delim"; ch: "*" | "_"; count: number; canOpen: boolean; canClose: boolean }
  | { t: "node"; node: InlineNode; depth: number };

/**
 * Model output is untrusted and its emphasis nesting is attacker-controlled.
 * Unbounded nesting recurses through mergeText and the renderer's node walk,
 * and `*`-dense text makes emphasis resolution quadratic — either one can
 * blank the whole app (React unmounts the root on a RangeError) or freeze
 * the UI mid-answer. Past these bounds the remaining delimiters render as
 * literal text, which is a cosmetic loss on input no real answer produces.
 */
const MAX_EMPHASIS_DEPTH = 24;
const MAX_EMPHASIS_NODES = 1000;
const MAX_EMPHASIS_CHARS = 20_000;

const PUNCT_RE = /[!-/:-@[-`{-~]/;

function isWs(ch: string): boolean {
  return ch === "" || /\s/.test(ch);
}

function isPunct(ch: string): boolean {
  return ch !== "" && PUNCT_RE.test(ch);
}

function scan(source: string): Token[] {
  const tokens: Token[] = [];
  let text = "";
  const flushText = () => {
    if (text) {
      tokens.push({ t: "text", s: text });
      text = "";
    }
  };
  let i = 0;
  const n = source.length;
  while (i < n) {
    const ch = source[i] ?? "";
    if (ch === "\\" && i + 1 < n && isPunct(source[i + 1] ?? "")) {
      text += source[i + 1] ?? "";
      i += 2;
      continue;
    }
    if (ch === "\n") {
      text += " "; // soft break renders as a space
      i += 1;
      continue;
    }
    if (ch === "`") {
      let runLen = 1;
      while (source[i + runLen] === "`") runLen += 1;
      // Closer must be a backtick run of EXACTLY the same length.
      let j = i + runLen;
      let close = -1;
      while (j < n) {
        if (source[j] === "`") {
          let closeLen = 1;
          while (source[j + closeLen] === "`") closeLen += 1;
          if (closeLen === runLen) {
            close = j;
            break;
          }
          j += closeLen;
        } else {
          j += 1;
        }
      }
      if (close === -1) {
        text += source.slice(i, i + runLen); // unclosed run stays literal
        i += runLen;
        continue;
      }
      flushText();
      let content = source.slice(i + runLen, close).replace(/\n/g, " ");
      if (
        content.length >= 2 &&
        content.startsWith(" ") &&
        content.endsWith(" ") &&
        content.trim() !== ""
      ) {
        content = content.slice(1, -1); // exactly one space of padding stripped
      }
      tokens.push({ t: "code", s: content });
      i = close + runLen;
      continue;
    }
    if (ch === "*" || ch === "_") {
      let count = 1;
      while (source[i + count] === ch) count += 1;
      const prev = i > 0 ? source[i - 1] ?? "" : "";
      const next = i + count < n ? source[i + count] ?? "" : "";
      const leftFlanking =
        !isWs(next) && (!isPunct(next) || isWs(prev) || isPunct(prev));
      const rightFlanking =
        !isWs(prev) && (!isPunct(prev) || isWs(next) || isPunct(next));
      let canOpen = leftFlanking;
      let canClose = rightFlanking;
      if (ch === "_") {
        // Intraword underscores never emphasize: snake_case stays literal.
        canOpen = leftFlanking && (!rightFlanking || isPunct(prev));
        canClose = rightFlanking && (!leftFlanking || isPunct(next));
      }
      flushText();
      tokens.push({ t: "delim", ch, count, canOpen, canClose });
      i += count;
      continue;
    }
    text += ch;
    i += 1;
  }
  flushText();
  return tokens;
}

function tokenToNodes(token: Token): InlineNode[] {
  switch (token.t) {
    case "text":
      return [{ kind: "text", text: token.s }];
    case "code":
      return [{ kind: "code", text: token.s }];
    case "node":
      return [token.node];
    case "delim":
      return token.count > 0
        ? [{ kind: "text", text: token.ch.repeat(token.count) }]
        : [];
  }
}

function resolveEmphasis(tokens: Token[]): InlineNode[] {
  const work: Token[] = tokens.slice();
  // Lowest index that can still hold an opener, per delimiter char. Without
  // it, every closer that finds no opener re-scans the whole array, which is
  // quadratic on delimiter-dense text.
  const openerFloor: Record<string, number> = { "*": 0, _: 0 };
  let resolved = 0;
  let closerIdx = 0;
  while (closerIdx < work.length) {
    const closer = work[closerIdx];
    if (!closer || closer.t !== "delim" || !closer.canClose || closer.count === 0) {
      closerIdx += 1;
      continue;
    }
    if (resolved >= MAX_EMPHASIS_NODES) break;
    // Nearest preceding opener of the same character.
    const floor = openerFloor[closer.ch] ?? 0;
    let openerIdx = -1;
    for (let k = closerIdx - 1; k >= floor; k -= 1) {
      const candidate = work[k];
      if (
        candidate &&
        candidate.t === "delim" &&
        candidate.ch === closer.ch &&
        candidate.canOpen &&
        candidate.count > 0
      ) {
        openerIdx = k;
        break;
      }
    }
    if (openerIdx === -1) {
      // This closer can still open for a later closer, so the floor includes it.
      openerFloor[closer.ch] = closerIdx;
      closerIdx += 1;
      continue;
    }
    const innerTokens = work.slice(openerIdx + 1, closerIdx);
    let childDepth = 0;
    for (const token of innerTokens) {
      if (token.t === "node" && token.depth > childDepth) childDepth = token.depth;
    }
    if (childDepth + 1 > MAX_EMPHASIS_DEPTH) {
      closerIdx += 1; // too deep to wrap — these delimiters stay literal
      continue;
    }
    const opener = work[openerIdx] as Extract<Token, { t: "delim" }>;
    const use = opener.count >= 2 && closer.count >= 2 ? 2 : 1;
    const children = innerTokens.flatMap(tokenToNodes);
    const node: InlineNode =
      use === 2 ? { kind: "strong", children } : { kind: "em", children };
    opener.count -= use;
    closer.count -= use;
    resolved += 1;
    const replacement: Token[] = [];
    if (opener.count > 0) replacement.push(opener);
    replacement.push({ t: "node", node, depth: childDepth + 1 });
    if (closer.count > 0) replacement.push(closer);
    work.splice(openerIdx, closerIdx - openerIdx + 1, ...replacement);
    // A splice shifts every index above openerIdx, so any floor above it is
    // now pointing at the wrong token. Floors at or below it are untouched.
    // Skipping this let a `*` pair invalidate a stale `_` floor and silently
    // swallow legitimate emphasis: "*the foo_ and bar_ x* use _trailing_ y".
    for (const ch of ["*", "_"]) {
      if ((openerFloor[ch] ?? 0) > openerIdx) openerFloor[ch] = openerIdx;
    }
    closerIdx = openerIdx; // re-scan from the replacement site
  }
  return work.flatMap(tokenToNodes);
}

/** Merge adjacent text nodes so the tree (and the DOM) is canonical. */
function mergeText(nodes: InlineNode[]): InlineNode[] {
  const out: InlineNode[] = [];
  for (const node of nodes) {
    const last = out[out.length - 1];
    if (node.kind === "text" && last && last.kind === "text") {
      out[out.length - 1] = { kind: "text", text: last.text + node.text };
    } else if (node.kind === "strong" || node.kind === "em") {
      out.push({ kind: node.kind, children: mergeText(node.children) });
    } else {
      out.push(node);
    }
  }
  return out;
}

export function parseInline(source: string): InlineNode[] {
  const tokens = scan(source);
  // Past the size guard, emphasis resolution is skipped entirely (delimiters
  // stay literal). Code spans and escapes still work, and every string is
  // still a text node — the security contract is unaffected.
  if (source.length > MAX_EMPHASIS_CHARS) return mergeText(tokens.flatMap(tokenToNodes));
  return mergeText(resolveEmphasis(tokens));
}
