import type { Span } from "../api/adapters/trace";

/**
 * Pure span-tree logic for the trace waterfall and span table.
 * Never mutates input; malformed/duplicate/cyclic data degrades to orphan
 * roots instead of crashing or dropping spans silently.
 */

export interface SpanNode {
  span: Span;
  depth: number;
  children: SpanNode[];
}

export interface SpanRow {
  span: Span;
  depth: number;
}

/** Sort key for span start time; unparseable timestamps sort last, stably. */
function startKey(span: Span): number {
  const t = Date.parse(span.startedAt);
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t;
}

/**
 * Build a forest of span nodes from a flat span list.
 * - Hierarchy comes from `parentSpanId`; a missing/unknown/self parent makes
 *   the span a root (orphan tolerance).
 * - Roots and children are sorted by `startedAt` (stable — equal or invalid
 *   timestamps keep input order).
 * - Duplicate span ids: the first occurrence owns the id; later duplicates
 *   become orphan roots (visible, never a crash).
 * - Cycles (a → b → a) cannot disappear: unreachable nodes are appended as
 *   roots after the reachability walk.
 */
export function buildSpanTree(spans: readonly Span[]): SpanNode[] {
  const byId = new Map<string, SpanNode>();
  const nodes: SpanNode[] = [];
  const registered = new Set<SpanNode>();

  for (const span of spans) {
    const node: SpanNode = { span, depth: 0, children: [] };
    nodes.push(node);
    if (!byId.has(span.spanId)) {
      byId.set(span.spanId, node);
      registered.add(node);
    }
  }

  const roots: SpanNode[] = [];
  for (const node of nodes) {
    const parentId = node.span.parentSpanId;
    const parent =
      parentId !== null && parentId !== node.span.spanId ? byId.get(parentId) : undefined;
    if (parent !== undefined && parent !== node && registered.has(node)) {
      parent.children.push(node);
    } else if (registered.has(node)) {
      roots.push(node);
    } else {
      // duplicate span id — surface as an orphan root
      roots.push(node);
    }
  }

  // Sort roots and children by startedAt (Array.sort is stable).
  const sortLevel = (level: SpanNode[]) => level.sort((a, b) => startKey(a.span) - startKey(b.span));
  sortLevel(roots);
  for (const node of nodes) sortLevel(node.children);

  // Assign depth via depth-first walk from roots; any node not reached
  // (only possible through a parent cycle) becomes a root too.
  const visited = new Set<SpanNode>();
  const walk = (node: SpanNode, depth: number) => {
    if (visited.has(node)) return;
    visited.add(node);
    node.depth = depth;
    for (const child of node.children) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  for (const node of nodes) {
    if (!visited.has(node)) {
      node.depth = 0;
      node.children = [];
      roots.push(node);
      walk(node, 0);
    }
  }
  return roots;
}

/** Depth-first flattening of the forest for table rendering. */
export function flattenSpanTree(roots: readonly SpanNode[]): SpanRow[] {
  const rows: SpanRow[] = [];
  const visit = (node: SpanNode, depth: number) => {
    rows.push({ span: node.span, depth });
    for (const child of node.children) visit(child, depth + 1);
  };
  for (const root of roots) visit(root, 0);
  return rows;
}
