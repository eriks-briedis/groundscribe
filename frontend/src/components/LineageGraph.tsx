/**
 * How one artefact came from another (phase 11).
 *
 * plan/11 → *Lineage graph — branching causal relationships between artefacts*.
 * Branching, not a list: supersession forks a child rather than overwriting a
 * parent (plan/00 → no silent mutation), so one version may have two successors
 * and a straight line would misdraw exactly the case worth looking at.
 *
 * Laid out by generation — depth from the root — with siblings spread across the
 * row. Nothing about the layout is meaningful except the edges; the graph is
 * small enough that anything cleverer would cost more than it explains.
 */
import type { LineageGraph as Graph } from '@/api/client';

const NODE_WIDTH = 120;
const ROW_HEIGHT = 90;

export interface LineageGraphProps {
  graph: Graph;
}

export function LineageGraph({ graph }: LineageGraphProps) {
  const nodes = graph.nodes ?? [];
  const edges = graph.edges ?? [];
  if (nodes.length === 0) {
    return <p className="lineage lineage--empty">Nothing has been produced yet.</p>;
  }

  const parents = new Map(edges.map((edge) => [edge.to, edge.from]));
  const depth = (id: string, seen = new Set<string>()): number => {
    const parent = parents.get(id);
    if (parent === undefined || seen.has(id)) return 0;
    seen.add(id);
    return 1 + depth(parent, seen);
  };

  const rows = new Map<number, string[]>();
  for (const node of nodes) {
    const level = depth(node.id);
    rows.set(level, [...(rows.get(level) ?? []), node.id]);
  }

  const position = (id: string): { x: number; y: number } => {
    const level = depth(id);
    const row = rows.get(level) ?? [];
    return { x: (row.indexOf(id) + 1) * NODE_WIDTH, y: (level + 1) * (ROW_HEIGHT / 2) };
  };

  const width = Math.max(...nodes.map((node) => position(node.id).x)) + NODE_WIDTH;
  const height = Math.max(...nodes.map((node) => position(node.id).y)) + 40;

  return (
    <svg
      className="lineage"
      data-testid="lineage"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${nodes.length} artefacts, ${edges.length} links`}
    >
      {edges.map((edge) => {
        const from = position(edge.from);
        const to = position(edge.to);
        return (
          <line
            key={`${edge.from}-${edge.to}`}
            data-edge={edge.kind}
            x1={from.x}
            y1={from.y}
            x2={to.x}
            y2={to.y}
            stroke="currentColor"
          />
        );
      })}
      {nodes.map((node) => {
        const { x, y } = position(node.id);
        return (
          <g key={node.id} data-node={node.kind} transform={`translate(${x} ${y})`}>
            <circle r={8} fill="currentColor" />
            <text x={12} y={4}>
              {node.label || node.id}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
