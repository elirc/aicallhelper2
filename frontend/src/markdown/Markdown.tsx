/**
 * The markdown renderer for model output. SECURITY-CRITICAL:
 * - every string reaches the DOM as a React text node — never
 *   dangerouslySetInnerHTML, never an attribute derived from model text;
 * - the parse is a pure function of the full source, so the streamed DOM is
 *   byte-identical to a batch render (tested over every cut point);
 * - completed blocks keep their DOM nodes across streaming updates: block
 *   index keys + per-block memo on a content signature.
 */
import { memo, useMemo, type ReactNode } from "react";

import { parseBlocks, blockSignature, type Block } from "./blocks";
import { parseInline, type InlineNode } from "./inline";

function renderInlineNodes(nodes: InlineNode[], keyPrefix: string): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${keyPrefix}.${index}`;
    switch (node.kind) {
      case "text":
        return node.text; // JSX text rendering = text node, always escaped
      case "code":
        return <code key={key}>{node.text}</code>;
      case "strong":
        return <strong key={key}>{renderInlineNodes(node.children, key)}</strong>;
      case "em":
        return <em key={key}>{renderInlineNodes(node.children, key)}</em>;
    }
  });
}

function Inline({ text }: { text: string }): ReactNode {
  const nodes = useMemo(() => parseInline(text), [text]);
  return <>{renderInlineNodes(nodes, "i")}</>;
}

function renderBlock(block: Block): ReactNode {
  switch (block.type) {
    case "paragraph":
      return (
        <p>
          <Inline text={block.text} />
        </p>
      );
    case "heading": {
      const Tag = `h${block.level}` as "h3" | "h4" | "h5" | "h6";
      return (
        <Tag>
          <Inline text={block.text} />
        </Tag>
      );
    }
    case "code":
      return (
        <pre>
          <code>{block.text}</code>
        </pre>
      );
    case "hr":
      return <hr />;
    case "list": {
      const items = block.items.map((item, index) => (
        <li key={index}>
          {block.loose ? (
            <p>
              <Inline text={item.text} />
            </p>
          ) : (
            <Inline text={item.text} />
          )}
        </li>
      ));
      return block.ordered ? <ol start={block.start}>{items}</ol> : <ul>{items}</ul>;
    }
  }
}

interface BlockViewProps {
  block: Block;
  signature: string;
}

const BlockView = memo(
  function BlockView({ block }: BlockViewProps) {
    return <>{renderBlock(block)}</>;
  },
  (prev, next) => prev.signature === next.signature,
);

export const Markdown = memo(function Markdown({ source }: { source: string }) {
  const blocks = useMemo(() => parseBlocks(source), [source]);
  return (
    <>
      {blocks.map((block, index) => (
        <BlockView key={index} block={block} signature={blockSignature(block)} />
      ))}
    </>
  );
});
