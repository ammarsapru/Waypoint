import {
  useCallback,
  useMemo,
  useRef,
  type PointerEvent as ReactPointerEvent,
} from "react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useViewport,
  type Edge,
  type Node,
} from "@xyflow/react";
import type {
  EdgeKind,
  GraphEdge,
  GraphNode,
  NodeKind,
  Selection,
  UnresolvedReference,
} from "../types";

const NODE_WIDTH = 218;
const NODE_HEIGHT = 66;
const FILE_NODE_SIZES = {
  compact: { width: 310, height: 230 },
  comfortable: { width: 390, height: 300 },
  large: { width: 470, height: 380 },
} as const;
const EXTERNAL_PACKAGES_PATH = "__waypoint__/external-packages";
const EXTERNAL_PACKAGES_NODE_ID = "__waypoint_external_packages__";
const MIN_ZOOM = 0.08;
const MAX_ZOOM = 1.8;

export type GraphPresentation = "symbols" | "files";
export type FileCardSize = keyof typeof FILE_NODE_SIZES;
export type GraphOrientation = "TB" | "LR";

const darkNodeColors: Record<NodeKind, { background: string; border: string }> = {
  repository: { background: "#17231f", border: "#50d890" },
  module: { background: "#17212c", border: "#5ea8ff" },
  class: { background: "#272117", border: "#e8b559" },
  function: { background: "#211a2c", border: "#a886f7" },
  method: { background: "#211a2c", border: "#d29cff" },
};

const lightNodeColors: Record<NodeKind, { background: string; border: string }> = {
  repository: { background: "#e9f8ef", border: "#168a50" },
  module: { background: "#eef6ff", border: "#2876c7" },
  class: { background: "#fff7e8", border: "#a86d08" },
  function: { background: "#f6f0ff", border: "#7546bd" },
  method: { background: "#f9efff", border: "#8a48b5" },
};

const edgeColors: Record<EdgeKind, string> = {
  contains: "#627080",
  imports: "#59b7d8",
  may_call: "#d99c4a",
  instantiates: "#d66fa9",
};

interface GraphViewProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  unresolvedReferences: UnresolvedReference[];
  visibleNodeKinds: Set<NodeKind>;
  visibleEdgeKinds: Set<EdgeKind>;
  search: string;
  selection: Selection;
  onSelect: (selection: Selection) => void;
  onExpand: (node: GraphNode) => void;
  theme: "light" | "dark";
  presentation: GraphPresentation;
  fileCardSize: FileCardSize;
  orientation: GraphOrientation;
}

interface FileGroup {
  path: string;
  primary: GraphNode;
  members: GraphNode[];
  uses: GraphNode[];
  externalPackages?: Array<{ name: string; files: string[] }>;
}

function nodePath(node: GraphNode): string | null {
  if (node.id === EXTERNAL_PACKAGES_NODE_ID) return EXTERNAL_PACKAGES_PATH;
  return node.span?.path ?? null;
}

function externalPackageGraph(
  nodes: GraphNode[],
  references: UnresolvedReference[],
): {
  node: GraphNode | null;
  edges: GraphEdge[];
  packages: Array<{ name: string; files: string[] }>;
} {
  const nodeIds = new Set(nodes.map((node) => node.id));
  const packages = new Map<string, Set<string>>();
  const external = references.filter(
    (reference) =>
      reference.reference_kind === "import" &&
      reference.metadata.external === true &&
      typeof reference.metadata.package === "string" &&
      reference.metadata.package.length > 0 &&
      nodeIds.has(reference.source),
  );
  for (const reference of external) {
    const name = reference.metadata.package as string;
    const files = packages.get(name) ?? new Set<string>();
    files.add(reference.evidence.span.path);
    packages.set(name, files);
  }
  if (packages.size === 0) return { node: null, edges: [], packages: [] };
  const node: GraphNode = {
    id: EXTERNAL_PACKAGES_NODE_ID,
    kind: "module",
    name: "External packages",
    qualified_name: "External packages",
    module: null,
    span: null,
    signature: null,
    metadata: { language: "dependencies", synthetic_kind: "external_packages" },
  };
  return {
    node,
    packages: [...packages.entries()]
      .map(([name, files]) => ({ name, files: [...files].sort() }))
      .sort((left, right) => left.name.localeCompare(right.name)),
    edges: external.map((reference, index) => ({
      id: `external:${reference.source}:${String(reference.metadata.package)}:${index}`,
      source: reference.source,
      target: EXTERNAL_PACKAGES_NODE_ID,
      kind: "imports",
      evidence: reference.evidence,
      metadata: {
        synthetic: true,
        external_package: reference.metadata.package,
        original_reference: reference.reference,
      },
    })),
  };
}

function fileGroups(
  nodes: GraphNode[],
  edges: GraphEdge[],
  visibleKinds: Set<NodeKind>,
  visibleEdgeKinds: Set<EdgeKind>,
  search: string,
  externalPackages: Array<{ name: string; files: string[] }> = [],
): FileGroup[] {
  const grouped = new Map<string, GraphNode[]>();
  for (const node of nodes) {
    const path = nodePath(node);
    if (!path || node.kind === "repository") continue;
    const current = grouped.get(path) ?? [];
    current.push(node);
    grouped.set(path, current);
  }
  const normalizedSearch = search.trim().toLowerCase();
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const pathByNode = new Map(
    nodes
      .filter((node) => nodePath(node))
      .map((node) => [node.id, nodePath(node) as string]),
  );
  const usesByPath = new Map<string, Map<string, GraphNode>>();
  for (const edge of edges) {
    if (!visibleEdgeKinds.has(edge.kind) || edge.kind === "contains") continue;
    const sourcePath = pathByNode.get(edge.source);
    const targetPath = pathByNode.get(edge.target);
    const target = nodesById.get(edge.target);
    if (!sourcePath || !targetPath || sourcePath === targetPath || !target) continue;
    const used = usesByPath.get(sourcePath) ?? new Map<string, GraphNode>();
    used.set(target.id, target);
    usesByPath.set(sourcePath, used);
  }
  return [...grouped.entries()]
    .map(([path, members]) => {
      const ordered = [...members].sort((left, right) => {
        if (left.kind === "module") return -1;
        if (right.kind === "module") return 1;
        return (left.span?.start_line ?? 0) - (right.span?.start_line ?? 0);
      });
      return {
        path,
        primary: ordered.find((node) => node.kind === "module") ?? ordered[0],
        members: ordered.filter(
          (node) =>
            visibleKinds.has(node.kind) &&
            (!normalizedSearch ||
              path.toLowerCase().includes(normalizedSearch) ||
              node.name.toLowerCase().includes(normalizedSearch) ||
              node.qualified_name.toLowerCase().includes(normalizedSearch)),
        ),
        uses: [...(usesByPath.get(path)?.values() ?? [])]
          .filter((node) => visibleKinds.has(node.kind))
          .sort((left, right) => left.qualified_name.localeCompare(right.qualified_name)),
        externalPackages:
          path === EXTERNAL_PACKAGES_PATH ? externalPackages : undefined,
      };
    })
    .filter((group) => {
      if (group.members.length > 0) return true;
      return Boolean(
        group.externalPackages?.some(
          (item) =>
            !normalizedSearch ||
            item.name.toLowerCase().includes(normalizedSearch) ||
            item.files.some((path) => path.toLowerCase().includes(normalizedSearch)),
        ),
      );
    })
    .sort((left, right) => left.path.localeCompare(right.path));
}

function layoutFileGraph(
  groups: FileGroup[],
  graphNodes: GraphNode[],
  graphEdges: GraphEdge[],
  visibleEdgeKinds: Set<EdgeKind>,
  theme: "light" | "dark",
  onSelect: (selection: Selection) => void,
  cardSize: FileCardSize,
  orientation: GraphOrientation,
): { nodes: Node[]; edges: Edge[] } {
  const { width: fileNodeWidth, height: fileNodeHeight } = FILE_NODE_SIZES[cardSize];
  const includedPaths = new Set(groups.map((group) => group.path));
  const pathByNode = new Map<string, string>();
  graphNodes.forEach((node) => {
    const path = nodePath(node);
    if (path) pathByNode.set(node.id, path);
  });
  const groupedEdges = new Map<string, GraphEdge[]>();
  for (const edge of graphEdges) {
    if (!visibleEdgeKinds.has(edge.kind)) continue;
    const sourcePath = pathByNode.get(edge.source);
    const targetPath = pathByNode.get(edge.target);
    if (
      !sourcePath ||
      !targetPath ||
      sourcePath === targetPath ||
      !includedPaths.has(sourcePath) ||
      !includedPaths.has(targetPath)
    ) continue;
    const key = `${sourcePath}\u0000${targetPath}\u0000${edge.kind}`;
    groupedEdges.set(key, [...(groupedEdges.get(key) ?? []), edge]);
  }

  const layout = new dagre.graphlib.Graph();
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({
    rankdir: orientation,
    nodesep: 54,
    ranksep: 120,
    marginx: 36,
    marginy: 36,
  });
  groups.forEach((group) =>
    layout.setNode(group.path, {
      width: fileNodeWidth,
      height: fileNodeHeight,
    }),
  );
  groupedEdges.forEach((_, key) => {
    const [source, target] = key.split("\u0000");
    layout.setEdge(source, target);
  });
  dagre.layout(layout);
  const colors = theme === "dark" ? darkNodeColors : lightNodeColors;

  return {
    nodes: groups.map((group) => {
      const position = layout.node(group.path);
      return {
        id: group.path,
        position: {
          x: position.x - fileNodeWidth / 2,
          y: position.y - fileNodeHeight / 2,
        },
        data: {
          original: group.primary,
          label: (
            <div className="file-graph-card">
              <div className={`file-card-heading ${group.externalPackages ? "external" : ""}`}>
                <span>{group.externalPackages ? "external" : String(group.primary.metadata.language ?? "file")}</span>
                <strong title={group.path}>{group.externalPackages ? "External packages" : group.path.split("/").at(-1)}</strong>
                <small>{group.externalPackages ? "Dependencies outside this repository" : group.path}</small>
              </div>
              <div className="file-card-members">
                {group.externalPackages ? (
                  <div className="external-package-list">
                    {group.externalPackages.map((item) => (
                      <div className="external-package-row" key={item.name}>
                        <strong>{item.name}</strong>
                        <span>{item.files.length} {item.files.length === 1 ? "file" : "files"}</span>
                        <small title={item.files.join(", ")}>{item.files.join(", ")}</small>
                      </div>
                    ))}
                  </div>
                ) : group.members.map((member) => (
                  <button
                    type="button"
                    className="nodrag nopan"
                    key={member.id}
                    title={member.qualified_name}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect({ type: "node", value: member });
                    }}
                  >
                    <span className={`member-kind ${member.kind}`}>{member.kind}</span>
                    <strong>{member.name}</strong>
                    {member.signature && <small>({member.signature})</small>}
                  </button>
                ))}
                {!group.externalPackages && group.uses.length > 0 && (
                  <div className="file-card-uses">
                    <span>Uses across files</span>
                    {group.uses.map((member) => (
                      <button
                        type="button"
                        className="nodrag nopan"
                        key={`uses:${member.id}`}
                        title={member.qualified_name}
                        onClick={(event) => {
                          event.stopPropagation();
                          onSelect({ type: "node", value: member });
                        }}
                      >
                        <span className={`member-kind ${member.kind}`}>{member.kind}</span>
                        <strong>{member.qualified_name}</strong>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="file-card-footer">
                {group.externalPackages
                  ? `${group.externalPackages.length} packages · ${new Set(group.externalPackages.flatMap((item) => item.files)).size} importing files`
                  : `${group.members.length} visible symbols · ${group.uses.length} external uses`}
              </div>
            </div>
          ),
        },
        style: {
          width: fileNodeWidth,
          height: fileNodeHeight,
          padding: 0,
          overflow: "hidden",
          color: theme === "dark" ? "#eef4f8" : "#17212b",
          background: colors.module.background,
          border: `1px solid ${colors.module.border}`,
          borderRadius: 9,
          boxShadow: "0 12px 32px rgba(0,0,0,.25)",
        },
      };
    }),
    edges: [...groupedEdges.entries()].map(([key, items]) => {
      const [source, target, kindValue] = key.split("\u0000");
      const kind = kindValue as EdgeKind;
      return {
        id: `file:${key}`,
        source,
        target,
        data: { original: items[0] },
        label: items.length > 1 ? String(items.length) : undefined,
        type: "smoothstep",
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: edgeColors[kind],
          width: 16,
          height: 16,
        },
        animated: kind === "imports",
        style: {
          stroke: edgeColors[kind],
          strokeWidth: Math.min(3.5, 1.5 + items.length * 0.12),
          strokeDasharray:
            kind === "may_call" || kind === "instantiates" ? "6 5" : undefined,
          opacity: 0.86,
        },
      };
    }),
  };
}

function layoutGraph(
  graphNodes: GraphNode[],
  graphEdges: GraphEdge[],
  theme: "light" | "dark",
  orientation: GraphOrientation,
): { nodes: Node[]; edges: Edge[] } {
  const nodeColors = theme === "dark" ? darkNodeColors : lightNodeColors;
  const layout = new dagre.graphlib.Graph();
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({
    rankdir: orientation,
    nodesep: 42,
    ranksep: 92,
    marginx: 32,
    marginy: 32,
  });
  graphNodes.forEach((node) => {
    layout.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });
  graphEdges.forEach((edge) => layout.setEdge(edge.source, edge.target));
  dagre.layout(layout);

  return {
    nodes: graphNodes.map((item) => {
      const position = layout.node(item.id);
      const colors = nodeColors[item.kind];
      return {
        id: item.id,
        position: {
          x: position.x - NODE_WIDTH / 2,
          y: position.y - NODE_HEIGHT / 2,
        },
        data: {
          label: (
            <div className="graph-node-label">
              <span className="graph-node-kind">{item.kind}</span>
              <strong title={item.qualified_name}>{item.name}</strong>
            </div>
          ),
          original: item,
        },
        style: {
          width: NODE_WIDTH,
          minHeight: NODE_HEIGHT,
          color: theme === "dark" ? "#eef4f8" : "#17212b",
          background: colors.background,
          border: `1px solid ${colors.border}`,
          borderLeft: `4px solid ${colors.border}`,
          borderRadius: 7,
          padding: "10px 12px",
          boxShadow: "0 8px 24px rgba(0,0,0,.22)",
        },
      };
    }),
    edges: graphEdges.map((item) => ({
      id: item.id,
      source: item.source,
      target: item.target,
      data: { original: item },
      type: "smoothstep",
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: edgeColors[item.kind],
        width: 16,
        height: 16,
      },
      animated: item.kind === "imports",
      style: {
        stroke: edgeColors[item.kind],
        strokeWidth: item.kind === "contains" ? 1.1 : 1.7,
        strokeDasharray:
          item.kind === "may_call" || item.kind === "instantiates"
            ? "6 5"
            : undefined,
        opacity: item.evidence.status === "inferred" ? 0.75 : 0.9,
      },
    })),
  };
}

// Log-scale mapping: zoom is multiplicative (0.08x-1.8x spans a >20x ratio), so an
// even split of the slider track by raw zoom value would waste most of its travel
// on the top of the range. Mapping through log() gives even, usable resolution
// across the whole range instead.
const LOG_MIN_ZOOM = Math.log(MIN_ZOOM);
const LOG_MAX_ZOOM = Math.log(MAX_ZOOM);

function zoomToFraction(zoom: number): number {
  const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
  return (Math.log(clamped) - LOG_MIN_ZOOM) / (LOG_MAX_ZOOM - LOG_MIN_ZOOM);
}

function fractionToZoom(fraction: number): number {
  const clamped = Math.min(1, Math.max(0, fraction));
  return Math.exp(LOG_MIN_ZOOM + clamped * (LOG_MAX_ZOOM - LOG_MIN_ZOOM));
}

/** Vertical drag-to-zoom scrubber overlaid on the canvas: drag the handle up to
 * zoom in, down to zoom out, or click anywhere on the track to jump there.
 * Reads/writes the same zoom state as scroll-wheel zoom and the +/- Controls
 * buttons, via ReactFlow's own viewport, so every zoom input stays in sync. */
function ZoomSlider() {
  const { setViewport, getViewport } = useReactFlow();
  const { zoom } = useViewport();
  const trackRef = useRef<HTMLDivElement>(null);

  const applyFromClientY = useCallback(
    (clientY: number) => {
      const track = trackRef.current;
      if (!track) return;
      const rect = track.getBoundingClientRect();
      // Top of the track = zoomed in (fraction 1), bottom = zoomed out (fraction 0).
      const fraction = 1 - (clientY - rect.top) / rect.height;
      setViewport(
        { ...getViewport(), zoom: fractionToZoom(fraction) },
        { duration: 0 },
      );
    },
    [getViewport, setViewport],
  );

  const handlePointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      applyFromClientY(event.clientY);
    },
    [applyFromClientY],
  );

  const handlePointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.buttons !== 1) return;
      applyFromClientY(event.clientY);
    },
    [applyFromClientY],
  );

  const handleTopPercent = (1 - zoomToFraction(zoom)) * 100;

  return (
    <Panel position="top-right" className="zoom-slider-panel">
      <div
        className="zoom-slider-track nodrag nopan nowheel"
        ref={trackRef}
        role="slider"
        aria-label="Zoom"
        aria-orientation="vertical"
        aria-valuemin={MIN_ZOOM}
        aria-valuemax={MAX_ZOOM}
        aria-valuenow={Math.round(zoom * 100) / 100}
        tabIndex={0}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp") {
            event.preventDefault();
            setViewport(
              { ...getViewport(), zoom: fractionToZoom(zoomToFraction(zoom) + 0.05) },
              { duration: 100 },
            );
          } else if (event.key === "ArrowDown") {
            event.preventDefault();
            setViewport(
              { ...getViewport(), zoom: fractionToZoom(zoomToFraction(zoom) - 0.05) },
              { duration: 100 },
            );
          }
        }}
      >
        <div className="zoom-slider-fill" style={{ height: `${(1 - handleTopPercent / 100) * 100}%` }} />
        <div
          className="zoom-slider-handle"
          style={{ top: `${handleTopPercent}%` }}
        />
      </div>
      <span className="zoom-slider-readout">{Math.round(zoom * 100)}%</span>
    </Panel>
  );
}

function GraphCanvas(props: GraphViewProps) {
  const rendered = useMemo(() => {
    if (props.presentation === "files") {
      const external = externalPackageGraph(
        props.nodes,
        props.unresolvedReferences,
      );
      const graphNodes = external.node
        ? [...props.nodes, external.node]
        : props.nodes;
      const graphEdges = [...props.edges, ...external.edges];
      const groups = fileGroups(
        graphNodes,
        graphEdges,
        props.visibleNodeKinds,
        props.visibleEdgeKinds,
        props.search,
        external.packages,
      );
      const limitedGroups = groups.slice(0, 250);
      return {
        ...layoutFileGraph(
          limitedGroups,
          graphNodes,
          graphEdges,
          props.visibleEdgeKinds,
          props.theme,
          props.onSelect,
          props.fileCardSize,
          props.orientation,
        ),
        totalCandidates: groups.length,
      };
    }
    const search = props.search.trim().toLowerCase();
    const kindVisible = props.nodes.filter((node) =>
      props.visibleNodeKinds.has(node.kind),
    );
    const searchMatches = new Set(
      kindVisible
        .filter(
          (node) =>
            !search ||
            node.name.toLowerCase().includes(search) ||
            node.qualified_name.toLowerCase().includes(search),
        )
        .map((node) => node.id),
    );
    const candidates = kindVisible.filter(
      (node) => !search || searchMatches.has(node.id),
    );
    const candidateIds = new Set(candidates.map((node) => node.id));
    const degree = new Map<string, number>();
    props.edges.forEach((edge) => {
      if (candidateIds.has(edge.source) && candidateIds.has(edge.target)) {
        degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
        degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
      }
    });
    const MAX_RENDERED_NODES = 400;
    const limitedCandidates =
      candidates.length <= MAX_RENDERED_NODES
        ? candidates
        : [...candidates]
            .sort((a, b) => {
              if (a.kind === "repository") return -1;
              if (b.kind === "repository") return 1;
              return (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0);
            })
            .slice(0, MAX_RENDERED_NODES);
    const includedIds = new Set(limitedCandidates.map((node) => node.id));
    const visibleEdges = props.edges.filter(
      (edge) =>
        props.visibleEdgeKinds.has(edge.kind) &&
        includedIds.has(edge.source) &&
        includedIds.has(edge.target),
    );
    return {
      ...layoutGraph(limitedCandidates, visibleEdges, props.theme, props.orientation),
      totalCandidates: candidates.length,
    };
  }, [
    props.nodes,
    props.edges,
    props.unresolvedReferences,
    props.visibleNodeKinds,
    props.visibleEdgeKinds,
    props.search,
    props.theme,
    props.presentation,
    props.fileCardSize,
    props.orientation,
    props.onSelect,
  ]);

  return (
    <ReactFlow
      key={`${props.presentation}-${props.fileCardSize}-${props.orientation}-${rendered.nodes.length}-${rendered.edges.length}-${props.search}`}
      nodes={rendered.nodes}
      edges={rendered.edges}
      fitView
      fitViewOptions={{ padding: 0.18, maxZoom: 1.15 }}
      minZoom={MIN_ZOOM}
      maxZoom={MAX_ZOOM}
      nodesDraggable
      onlyRenderVisibleElements={rendered.nodes.length > 150}
      onPaneClick={() => props.onSelect(null)}
      onNodeClick={(_, node) =>
        props.onSelect({
          type: "node",
          value: node.data.original as GraphNode,
        })
      }
      onNodeDoubleClick={(_, node) => {
        const original = node.data.original as GraphNode;
        if (original.metadata.synthetic_kind !== "external_packages") {
          props.onExpand(original);
        }
      }}
      onEdgeClick={(_, edge) =>
        props.onSelect({
          type: "edge",
          value: edge.data?.original as GraphEdge,
        })
      }
      proOptions={{ hideAttribution: true }}
      colorMode={props.theme}
    >
      <Background
        variant={BackgroundVariant.Dots}
        gap={22}
        size={1}
        color="#33404c"
      />
      <Controls showInteractive={false} />
      <ZoomSlider />
      <Panel position="top-left" className="canvas-count">
        {rendered.nodes.length}
        {rendered.totalCandidates > rendered.nodes.length
          ? ` of ${rendered.totalCandidates}`
          : ""}{" "}
        nodes · {rendered.edges.length} edges visible
      </Panel>
      <Panel position="bottom-center" className="canvas-count">
        {props.presentation === "files"
          ? "Select a symbol inside a file card to inspect its source"
          : "Double-click a node to load its immediate neighborhood"}
      </Panel>
    </ReactFlow>
  );
}

export function GraphView(props: GraphViewProps) {
  return (
    <ReactFlowProvider>
      <GraphCanvas {...props} />
    </ReactFlowProvider>
  );
}
